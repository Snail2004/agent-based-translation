from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.alignment_manifest_v1 import (
    ALIGNMENT_MANIFEST_SCHEMA_ID,
    ALIGNMENT_MANIFEST_SCHEMA_VERSION,
    AlignmentTargetSegmentV1,
    AlignmentTargetSnapshotV1,
    alignment_source_read_model_sha256,
    build_alignment_target_snapshot,
    make_alignment_target_segment,
    seal_alignment_manifest,
    validate_alignment_bindings,
)
from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import CanonicalPolicy, canonical_sha256


__all__ = [
    "COMMUNITY_ARM_ID",
    "D2LCommunityAlignmentError",
    "D2LCommunityTargetFileV1",
    "D2LCommunityTargetReadModelV1",
    "D2LOriginStructuralRowV1",
    "D2LStructuralAuditDispositionV1",
    "D2LStructuralAuditPlanV1",
    "D2LStructuralAuditSelectionV1",
    "D2LTargetStructuralRowV1",
    "build_d2l_community_target_read_model",
    "build_d2l_structural_audit_plan",
    "build_d2l_structural_review_manifest",
    "resolve_d2l_structural_audit_sections",
]


COMMUNITY_ARM_ID = "community_unverified"
_TARGET_ARTIFACT_SCHEMA_ID = "D2LCommunityTargetArtifactV1"
_TARGET_ARTIFACT_SCHEMA_VERSION = "1.0.0"
_AUDIT_POLICY_ID = "d2l_structural_alignment_audit_v1"
_AUDIT_POLICY_VERSION = "1.0.0"
_AUDIT_MINIMUM = 30
_AUDIT_FRACTION = 0.10
_COMPARABLE_BLOCK_TYPES = frozenset({"heading", "prose"})
_ELIGIBLE_ADMISSIONS = frozenset({"translate", "translate_structured"})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_BLOCK_SUFFIX = re.compile(r"^(?P<section>.+)_b(?P<number>[0-9]{3,})$")
_PLAIN_SECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_TARGET_ARTIFACT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("files",)}),
)

_AUDIT_SELECTION_POLICY = CanonicalPolicy(
    set_like_paths=frozenset({("selections", "*", "selection_reasons")}),
    semantic_sequence_paths=frozenset({("selections",)}),
)

_AUDIT_SEED_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(), semantic_sequence_paths=frozenset()
)


class D2LCommunityAlignmentError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class D2LCommunityTargetFileV1:
    relative_path: str
    section_slug: str
    section_order: int
    file_sha256: str


@dataclass(frozen=True, slots=True)
class D2LTargetStructuralRowV1:
    segment_id: str
    chapter_id: str
    section_slug: str
    section_order: int
    block_order_in_section: int
    block_type: str


@dataclass(frozen=True, slots=True)
class D2LOriginStructuralRowV1:
    chapter_id: str
    section_slug: str
    section_order: int
    block_order_in_section: int
    block_type: str
    source_text: str
    source_text_sha256: str


@dataclass(frozen=True, slots=True)
class D2LStructuralAuditSelectionV1:
    mapping_id: str
    section_slug: str
    source_block_id: str
    target_segment_id: str
    selection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class D2LStructuralAuditPlanV1:
    policy_id: str
    policy_version: str
    source_read_model_sha256: str
    target_segments_sha256: str
    origin_files_sha256: str
    population_count: int
    sample_count: int
    selection_sha256: str
    selections: tuple[D2LStructuralAuditSelectionV1, ...]


@dataclass(frozen=True, slots=True)
class D2LStructuralAuditDispositionV1:
    accepted_sections: tuple[str, ...]
    review_required_sections: tuple[str, ...]
    failed_mapping_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class D2LCommunityTargetReadModelV1:
    repository_commit: str
    files_sha256: str
    files: tuple[D2LCommunityTargetFileV1, ...]
    origin_files_sha256: str
    origin_files: tuple[D2LCommunityTargetFileV1, ...]
    origin_structural_rows: tuple[D2LOriginStructuralRowV1, ...]
    structural_rows: tuple[D2LTargetStructuralRowV1, ...]
    snapshot: AlignmentTargetSnapshotV1


def build_d2l_community_target_read_model(
    chapter_dir: str | Path,
    *,
    repository_commit: str,
    artifact_id: str,
    project_id: str,
    document_id: str,
    chapter_id: str,
) -> D2LCommunityTargetReadModelV1:
    root = Path(chapter_dir)
    if not root.is_dir():
        raise D2LCommunityAlignmentError(
            "chapter_directory", f"target chapter directory does not exist: {root}"
        )
    if not _HEX40.fullmatch(repository_commit):
        raise D2LCommunityAlignmentError(
            "repository_commit", "repository commit must be 40 lowercase hex characters"
        )

    section_files = _target_section_files_in_order(root)
    if not section_files:
        raise D2LCommunityAlignmentError(
            "target_sections", "target chapter contains no Markdown sections"
        )

    files: list[D2LCommunityTargetFileV1] = []
    origin_files: list[D2LCommunityTargetFileV1] = []
    origin_structural_rows: list[D2LOriginStructuralRowV1] = []
    structural_rows: list[D2LTargetStructuralRowV1] = []
    segments: list[AlignmentTargetSegmentV1] = []
    global_order = 0
    seen_segment_ids: set[str] = set()

    for section_order, section_path in enumerate(section_files):
        section_slug = _slug(section_path.stem)
        origin_path = section_path.with_name(f"{section_path.stem}_origin.md")
        if not origin_path.is_file():
            raise D2LCommunityAlignmentError(
                "origin_section",
                f"target section has no pinned origin sibling: {section_path.name}",
            )
        relative_path = f"{root.name}/{section_path.name}"
        files.append(
            D2LCommunityTargetFileV1(
                relative_path=relative_path,
                section_slug=section_slug,
                section_order=section_order,
                file_sha256=_sha256_bytes(section_path.read_bytes()),
            )
        )
        origin_files.append(
            D2LCommunityTargetFileV1(
                relative_path=f"{root.name}/{origin_path.name}",
                section_slug=section_slug,
                section_order=section_order,
                file_sha256=_sha256_bytes(origin_path.read_bytes()),
            )
        )
        origin_text = origin_path.read_text(encoding="utf-8")
        for block_order, block_text in enumerate(_split_markdown_blocks(origin_text)):
            block_type = _classify_block(block_text)
            if block_type not in _COMPARABLE_BLOCK_TYPES:
                continue
            origin_structural_rows.append(
                D2LOriginStructuralRowV1(
                    chapter_id=chapter_id,
                    section_slug=section_slug,
                    section_order=section_order,
                    block_order_in_section=block_order,
                    block_type=block_type,
                    source_text=block_text,
                    source_text_sha256=_sha256_bytes(block_text.encode("utf-8")),
                )
            )
        text = section_path.read_text(encoding="utf-8")
        for block_order, block_text in enumerate(_split_markdown_blocks(text)):
            block_type = _classify_block(block_text)
            if block_type not in _COMPARABLE_BLOCK_TYPES:
                continue
            segment_id = (
                f"community__{chapter_id}__{section_slug}__t{block_order + 1:03d}"
            )
            if segment_id in seen_segment_ids:
                raise D2LCommunityAlignmentError(
                    "duplicate_segment_id", f"duplicate target segment: {segment_id}"
                )
            seen_segment_ids.add(segment_id)
            segments.append(
                make_alignment_target_segment(
                    segment_id=segment_id,
                    chapter_id=chapter_id,
                    order_index=global_order,
                    text=block_text,
                )
            )
            structural_rows.append(
                D2LTargetStructuralRowV1(
                    segment_id=segment_id,
                    chapter_id=chapter_id,
                    section_slug=section_slug,
                    section_order=section_order,
                    block_order_in_section=block_order,
                    block_type=block_type,
                )
            )
            global_order += 1

    if not segments:
        raise D2LCommunityAlignmentError(
            "target_segments", "target chapter has no comparable heading or prose blocks"
        )

    files_payload = [
        {
            "relative_path": row.relative_path,
            "section_slug": row.section_slug,
            "section_order": row.section_order,
            "file_sha256": row.file_sha256,
        }
        for row in files
    ]
    files_sha256 = canonical_sha256(
        {"files": files_payload}, policy=_TARGET_ARTIFACT_POLICY
    )
    origin_files_payload = [
        {
            "relative_path": row.relative_path,
            "section_slug": row.section_slug,
            "section_order": row.section_order,
            "file_sha256": row.file_sha256,
        }
        for row in origin_files
    ]
    origin_files_sha256 = canonical_sha256(
        {"files": origin_files_payload}, policy=_TARGET_ARTIFACT_POLICY
    )
    artifact_sha256 = canonical_sha256(
        {
            "schema_id": _TARGET_ARTIFACT_SCHEMA_ID,
            "schema_version": _TARGET_ARTIFACT_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "project_id": project_id,
            "document_id": document_id,
            "chapter_id": chapter_id,
            "arm_id": COMMUNITY_ARM_ID,
            "repository_commit": repository_commit,
            "files_sha256": files_sha256,
            "files": files_payload,
        },
        policy=_TARGET_ARTIFACT_POLICY,
    )
    snapshot = build_alignment_target_snapshot(
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        project_id=project_id,
        document_id=document_id,
        arm_id=COMMUNITY_ARM_ID,
        source_language="en",
        target_language="vi",
        segments=segments,
    )
    return D2LCommunityTargetReadModelV1(
        repository_commit=repository_commit,
        files_sha256=files_sha256,
        files=tuple(files),
        origin_files_sha256=origin_files_sha256,
        origin_files=tuple(origin_files),
        origin_structural_rows=tuple(origin_structural_rows),
        structural_rows=tuple(structural_rows),
        snapshot=snapshot,
    )


def build_d2l_structural_review_manifest(
    common: CommonEvaluationInputV1,
    target: D2LCommunityTargetReadModelV1,
    *,
    manifest_id: str,
    created_at: str,
    producer_code_commit: str,
    implementation_commit: str,
) -> dict[str, Any]:
    _require_commit(producer_code_commit, field="producer_code_commit")
    _require_commit(implementation_commit, field="implementation_commit")

    chapter_ids = {block.chapter_id for block in common.blocks}
    if len(chapter_ids) != 1:
        raise D2LCommunityAlignmentError(
            "source_chapter_scope", "pilot common input must contain exactly one chapter"
        )
    chapter_id = next(iter(chapter_ids))
    if {row.chapter_id for row in target.structural_rows} != {chapter_id}:
        raise D2LCommunityAlignmentError(
            "target_chapter_scope", "target rows belong to a different chapter"
        )
    _validate_origin_binding(common, target, chapter_id=chapter_id)

    source_rows: list[tuple[str, str, int, str]] = []
    for block in common.blocks:
        if block.admission not in _ELIGIBLE_ADMISSIONS:
            continue
        section_slug, block_order = _parse_source_block_address(
            block.block_id, chapter_id=block.chapter_id
        )
        if block.block_type not in _COMPARABLE_BLOCK_TYPES:
            raise D2LCommunityAlignmentError(
                "source_block_type",
                f"eligible source block {block.block_id} has unsupported type "
                f"{block.block_type}",
            )
        source_rows.append(
            (block.block_id, section_slug, block_order, block.block_type)
        )

    if not source_rows:
        raise D2LCommunityAlignmentError(
            "source_segments", "source chapter has no eligible comparison blocks"
        )

    target_by_key: dict[tuple[str, int, str], D2LTargetStructuralRowV1] = {}
    for row in target.structural_rows:
        key = (row.section_slug, row.block_order_in_section, row.block_type)
        if key in target_by_key:
            raise D2LCommunityAlignmentError(
                "duplicate_target_position", f"duplicate target structural key: {key}"
            )
        target_by_key[key] = row

    source_keys = [(section, order, block_type) for _, section, order, block_type in source_rows]
    target_keys = [
        (row.section_slug, row.block_order_in_section, row.block_type)
        for row in target.structural_rows
    ]
    if source_keys != target_keys:
        _raise_structural_mismatch(source_keys, target_keys)

    mappings: list[dict[str, Any]] = []
    for index, (block_id, section, order, block_type) in enumerate(source_rows):
        target_row = target_by_key[(section, order, block_type)]
        mappings.append(
            {
                "mapping_id": f"d2l_structural_review_{index + 1:04d}",
                "chapter_id": chapter_id,
                "mapping_kind": "1:1",
                "decision_state": "review_required",
                "confidence": None,
                "source_block_ids": [block_id],
                "target_segment_ids": [target_row.segment_id],
                "decision_artifact_id": None,
                "decision_artifact_sha256": None,
            }
        )

    count = len(mappings)
    manifest = seal_alignment_manifest(
        {
            "schema_id": ALIGNMENT_MANIFEST_SCHEMA_ID,
            "schema_version": ALIGNMENT_MANIFEST_SCHEMA_VERSION,
            "manifest_id": manifest_id,
            "created_at": created_at,
            "producer": {
                "workstream": "evaluation",
                "component": "d2l_community_structural_alignment",
                "component_version": "1.0.0",
                "code_commit": producer_code_commit,
            },
            "source_read_model": {
                "project_id": common.project_id,
                "document_id": common.document_id,
                "source_schema_id": common.source_schema_id,
                "source_schema_version": common.source_schema_version,
                "source_read_model_sha256": alignment_source_read_model_sha256(
                    common
                ),
                "eligible_source_block_count": count,
            },
            "target_snapshot": {
                "artifact_id": target.snapshot.artifact_id,
                "artifact_sha256": target.snapshot.artifact_sha256,
                "project_id": target.snapshot.project_id,
                "document_id": target.snapshot.document_id,
                "arm_id": target.snapshot.arm_id,
                "source_language": target.snapshot.source_language,
                "target_language": target.snapshot.target_language,
                "segments_sha256": target.snapshot.segments_sha256,
                "target_segment_count": count,
            },
            "method": {
                "method_id": "d2l_sectionwise_structure_review",
                "method_version": "1.0.0",
                "implementation_commit": implementation_commit,
                "prompt_version": None,
                "model_id": None,
            },
            "mappings": mappings,
            "coverage": _all_review_coverage(count),
            "integrity": {"manifest_sha256": "0" * 64},
        }
    )
    return validate_alignment_bindings(manifest, common, target.snapshot)


def build_d2l_structural_audit_plan(
    review_manifest: Mapping[str, Any],
    common: CommonEvaluationInputV1,
    target: D2LCommunityTargetReadModelV1,
) -> D2LStructuralAuditPlanV1:
    manifest = validate_alignment_bindings(
        review_manifest, common, target.snapshot
    )
    mappings = manifest["mappings"]
    if any(
        row["mapping_kind"] != "1:1"
        or row["decision_state"] != "review_required"
        for row in mappings
    ):
        raise D2LCommunityAlignmentError(
            "audit_population",
            "structural audit population must contain only review-held 1:1 mappings",
        )

    chapter_id = next(iter({block.chapter_id for block in common.blocks}))
    section_by_mapping: dict[str, str] = {}
    section_mapping_ids: dict[str, list[str]] = {}
    for row in mappings:
        source_block_id = row["source_block_ids"][0]
        section_slug, _ = _parse_source_block_address(
            source_block_id, chapter_id=chapter_id
        )
        mapping_id = row["mapping_id"]
        section_by_mapping[mapping_id] = section_slug
        section_mapping_ids.setdefault(section_slug, []).append(mapping_id)

    reasons_by_mapping: dict[str, set[str]] = {}
    for mapping_ids in section_mapping_ids.values():
        reasons_by_mapping.setdefault(mapping_ids[0], set()).add("section_first")
        reasons_by_mapping.setdefault(mapping_ids[-1], set()).add("section_last")

    population_count = len(mappings)
    sample_count = min(
        population_count,
        max(
            _AUDIT_MINIMUM,
            math.ceil(population_count * _AUDIT_FRACTION),
            len(reasons_by_mapping),
        ),
    )
    seed = canonical_sha256(
        {
            "policy_id": _AUDIT_POLICY_ID,
            "policy_version": _AUDIT_POLICY_VERSION,
            "source_read_model_sha256": manifest["source_read_model"][
                "source_read_model_sha256"
            ],
            "target_segments_sha256": manifest["target_snapshot"][
                "segments_sha256"
            ],
            "origin_files_sha256": target.origin_files_sha256,
        },
        policy=_AUDIT_SEED_POLICY,
    )
    required = set(reasons_by_mapping)
    remaining = [row for row in mappings if row["mapping_id"] not in required]
    remaining.sort(
        key=lambda row: hashlib.sha256(
            f"{seed}\0{row['mapping_id']}".encode("utf-8")
        ).hexdigest()
    )
    for row in remaining[: sample_count - len(required)]:
        reasons_by_mapping.setdefault(row["mapping_id"], set()).add(
            "deterministic_hash"
        )

    selections: list[D2LStructuralAuditSelectionV1] = []
    for row in mappings:
        mapping_id = row["mapping_id"]
        reasons = reasons_by_mapping.get(mapping_id)
        if not reasons:
            continue
        selections.append(
            D2LStructuralAuditSelectionV1(
                mapping_id=mapping_id,
                section_slug=section_by_mapping[mapping_id],
                source_block_id=row["source_block_ids"][0],
                target_segment_id=row["target_segment_ids"][0],
                selection_reasons=tuple(sorted(reasons)),
            )
        )

    selection_payload = [
        {
            "mapping_id": row.mapping_id,
            "section_slug": row.section_slug,
            "source_block_id": row.source_block_id,
            "target_segment_id": row.target_segment_id,
            "selection_reasons": list(row.selection_reasons),
        }
        for row in selections
    ]
    selection_sha256 = canonical_sha256(
        {"selections": selection_payload}, policy=_AUDIT_SELECTION_POLICY
    )
    return D2LStructuralAuditPlanV1(
        policy_id=_AUDIT_POLICY_ID,
        policy_version=_AUDIT_POLICY_VERSION,
        source_read_model_sha256=manifest["source_read_model"][
            "source_read_model_sha256"
        ],
        target_segments_sha256=manifest["target_snapshot"]["segments_sha256"],
        origin_files_sha256=target.origin_files_sha256,
        population_count=population_count,
        sample_count=len(selections),
        selection_sha256=selection_sha256,
        selections=tuple(selections),
    )


def resolve_d2l_structural_audit_sections(
    audit_plan: D2LStructuralAuditPlanV1,
    outcomes: Mapping[str, bool],
) -> D2LStructuralAuditDispositionV1:
    expected_ids = [row.mapping_id for row in audit_plan.selections]
    actual_ids = list(outcomes)
    missing = sorted(set(expected_ids) - set(actual_ids))
    foreign = sorted(set(actual_ids) - set(expected_ids))
    if missing or foreign or len(actual_ids) != len(set(actual_ids)):
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if foreign:
            details.append("foreign=" + ",".join(foreign))
        if len(actual_ids) != len(set(actual_ids)):
            details.append("duplicate outcome IDs")
        raise D2LCommunityAlignmentError(
            "audit_exact_cover", "; ".join(details) or "audit outcomes differ"
        )
    non_boolean = [
        mapping_id
        for mapping_id, value in outcomes.items()
        if not isinstance(value, bool)
    ]
    if non_boolean:
        raise D2LCommunityAlignmentError(
            "audit_outcome_type",
            "audit outcomes must be booleans: " + ",".join(non_boolean),
        )

    section_by_mapping = {
        row.mapping_id: row.section_slug for row in audit_plan.selections
    }
    failed_mapping_ids = tuple(
        mapping_id for mapping_id in expected_ids if not outcomes[mapping_id]
    )
    failed_sections = {
        section_by_mapping[mapping_id] for mapping_id in failed_mapping_ids
    }
    section_order = list(
        dict.fromkeys(row.section_slug for row in audit_plan.selections)
    )
    return D2LStructuralAuditDispositionV1(
        accepted_sections=tuple(
            section for section in section_order if section not in failed_sections
        ),
        review_required_sections=tuple(
            section for section in section_order if section in failed_sections
        ),
        failed_mapping_ids=failed_mapping_ids,
    )


def _target_section_files_in_order(chapter_dir: Path) -> list[Path]:
    target_files = {
        path.stem: path
        for path in chapter_dir.glob("*.md")
        if not path.name.endswith("_origin.md")
    }
    ordered: list[Path] = []
    index_path = chapter_dir / "index.md"
    if index_path.exists():
        ordered.append(index_path)
        for line in index_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(":") or stripped.startswith("`"):
                continue
            if _PLAIN_SECTION_NAME.fullmatch(stripped):
                candidate = target_files.get(stripped)
                if candidate is not None and candidate not in ordered:
                    ordered.append(candidate)
    for path in sorted(target_files.values(), key=lambda item: item.name):
        if path not in ordered:
            ordered.append(path)
    return ordered


def _split_markdown_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    in_math = False

    def flush() -> None:
        nonlocal current
        value = "\n".join(current).strip()
        if value:
            blocks.append(value)
        current = []

    for line in text.splitlines():
        stripped = line.strip()
        fence = stripped.startswith("```") or stripped.startswith("~~~")
        math = stripped == "$$"
        if not stripped and not in_fence and not in_math:
            flush()
            continue
        current.append(line)
        if fence:
            in_fence = not in_fence
        elif math:
            in_math = not in_math
    flush()
    return blocks


def _classify_block(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    if first.startswith("#"):
        return "heading"
    if first.startswith("```") or first.startswith("~~~"):
        return "code"
    if first == "$$":
        return "math_block"
    if first.startswith("!["):
        return "image"
    if first.startswith((":label:", ":numref:", ":eqlabel:")):
        return "label"
    return "prose"


def _parse_source_block_address(block_id: str, *, chapter_id: str) -> tuple[str, int]:
    prefix = chapter_id + "_"
    if not block_id.startswith(prefix):
        raise D2LCommunityAlignmentError(
            "source_block_address",
            f"block {block_id} does not begin with chapter prefix {prefix}",
        )
    match = _SOURCE_BLOCK_SUFFIX.fullmatch(block_id[len(prefix) :])
    if match is None:
        raise D2LCommunityAlignmentError(
            "source_block_address", f"block has no D2L section/bNNN suffix: {block_id}"
        )
    return match.group("section"), int(match.group("number")) - 1


def _validate_origin_binding(
    common: CommonEvaluationInputV1,
    target: D2LCommunityTargetReadModelV1,
    *,
    chapter_id: str,
) -> None:
    source_rows: list[tuple[tuple[str, int, str], str, str]] = []
    for block in common.blocks:
        if block.admission not in _ELIGIBLE_ADMISSIONS:
            continue
        section_slug, block_order = _parse_source_block_address(
            block.block_id, chapter_id=chapter_id
        )
        source_rows.append(
            (
                (section_slug, block_order, block.block_type),
                block.block_id,
                block.source_text,
            )
        )
    origin_rows = [
        (
            (row.section_slug, row.block_order_in_section, row.block_type),
            row.source_text,
        )
        for row in target.origin_structural_rows
    ]
    source_keys = [row[0] for row in source_rows]
    origin_keys = [row[0] for row in origin_rows]
    if source_keys != origin_keys:
        _raise_named_structural_mismatch(
            source_keys,
            origin_keys,
            code="origin_structural_mismatch",
            left_name="source",
            right_name="origin",
        )
    for (_, block_id, source_text), (_, origin_text) in zip(
        source_rows, origin_rows, strict=True
    ):
        if source_text != origin_text:
            raise D2LCommunityAlignmentError(
                "origin_source_text",
                f"source text differs from pinned origin bytes for {block_id}",
            )


def _raise_structural_mismatch(
    source_keys: Sequence[tuple[str, int, str]],
    target_keys: Sequence[tuple[str, int, str]],
) -> None:
    _raise_named_structural_mismatch(
        source_keys,
        target_keys,
        code="structural_mismatch",
        left_name="source",
        right_name="target",
    )


def _raise_named_structural_mismatch(
    left_keys: Sequence[tuple[str, int, str]],
    right_keys: Sequence[tuple[str, int, str]],
    *,
    code: str,
    left_name: str,
    right_name: str,
) -> None:
    limit = min(len(left_keys), len(right_keys))
    first_difference = next(
        (index for index in range(limit) if left_keys[index] != right_keys[index]),
        limit,
    )
    left_value = (
        left_keys[first_difference] if first_difference < len(left_keys) else None
    )
    right_value = (
        right_keys[first_difference] if first_difference < len(right_keys) else None
    )
    raise D2LCommunityAlignmentError(
        code,
        f"{left_name} and {right_name} structural sequences differ at index "
        f"{first_difference}: {left_name}={left_value}, {right_name}={right_value}; "
        f"counts={len(left_keys)}/{len(right_keys)}",
    )


def _all_review_coverage(count: int) -> dict[str, int]:
    return {
        "source_block_count": count,
        "target_segment_count": count,
        "accepted_mapping_count": 0,
        "review_mapping_count": count,
        "ambiguous_mapping_count": 0,
        "missing_mapping_count": 0,
        "added_mapping_count": 0,
        "accepted_source_block_count": 0,
        "review_source_block_count": count,
        "ambiguous_source_block_count": 0,
        "missing_source_block_count": 0,
        "accepted_target_segment_count": 0,
        "review_target_segment_count": count,
        "ambiguous_target_segment_count": 0,
        "added_target_segment_count": 0,
    }


def _require_commit(value: str, *, field: str) -> None:
    if not _HEX40.fullmatch(value):
        raise D2LCommunityAlignmentError(
            field, f"{field} must be 40 lowercase hex characters"
        )


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")
    if not slug:
        raise D2LCommunityAlignmentError("section_slug", "empty section slug")
    return slug


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
