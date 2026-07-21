from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pipeline.ingest.admitted_projection import (
    validate_admitted_projection,
)
from pipeline.ingest.canonical_source_package import (
    canonical_json_sha256,
    validate_canonical_source_package,
)
from pipeline.ingest.source_package_materializer import materialize_source_package
from pipeline.ingest.unified_source_normalizer import (
    validate_normalization_contract,
)


DRAFT_PROJECT_STATE_VERSION = "draft_project_state_v1"
DRAFT_STRUCTURE_REPORT_VERSION = "draft_structure_report_v2"
GLOBAL_STRUCTURE_SKELETON_VERSION = "draft_structure_global_skeleton_v1"
GLOBAL_STRUCTURE_POLICY_VERSION = "draft_structure_global_policy_v1"
CORRECTION_PLAN_VERSION = "draft_structure_correction_plan_v1"
CORRECTION_RECEIPT_VERSION = "draft_structure_correction_receipt_v1"
CORRECTION_ENGINE_VERSION = "draft_structure_correction_v1"
HIERARCHY_PLAN_VERSION = "draft_structure_hierarchy_plan_v1"
HIERARCHY_OVERLAY_VERSION = "draft_structure_hierarchy_overlay_v1"
HIERARCHY_STATE = "experimental_non_load_bearing"

CLASSIFICATIONS = ("translate", "preserve", "exclude", "review")
PROPOSER_KINDS = ("human", "llm", "synthetic")
ACTION_TYPES = ("update_unit", "split_unit", "merge_adjacent_units")
HIERARCHY_ACTION_TYPES = ("set_parent", "clear_parent")

_PROJECT_STATE_FIELDS = {
    "schema_version",
    "doc_id",
    "lifecycle",
    "pipeline_run_count",
}
_REPORT_FIELDS = {
    "schema_version",
    "doc_id",
    "editable",
    "inputs",
    "units",
    "issues",
    "global_skeleton",
    "integrity",
}
_REPORT_INPUT_FIELDS = {
    "source",
    "document",
    "structure",
    "asset_manifest",
    "admitted_projection",
    "project_state",
}
_IDENTITY_FIELDS = {"schema_version", "sha256"}
_REPORT_UNIT_FIELDS = {
    "unit_id",
    "chapter_id",
    "order_index",
    "title",
    "block_ids",
    "role",
    "translation_policy",
    "confidence",
    "review_required",
    "issue_codes",
}
_ISSUE_FIELDS = {
    "issue_id",
    "code",
    "scope",
    "target_id",
    "evidence",
}
_REPORT_INTEGRITY_FIELDS = {"unit_count", "issue_count", "payload_sha256"}
_GLOBAL_SKELETON_FIELDS = {
    "schema_version",
    "doc_id",
    "inputs",
    "policy",
    "outline",
    "navigation",
    "candidates",
    "issues",
    "statistics",
    "integrity",
}
_GLOBAL_SKELETON_INPUT_FIELDS = {
    "document",
    "structure",
    "asset_manifest",
    "admitted_projection",
    "policy",
}
_GLOBAL_OUTLINE_FIELDS = {
    "unit_id",
    "chapter_id",
    "order_index",
    "title",
    "first_block_id",
    "last_block_id",
    "block_count",
    "parent_unit_id",
}
_GLOBAL_NAVIGATION_FIELDS = {
    "entry_id",
    "order_index",
    "title",
    "normalized_title",
    "depth",
    "parent_id",
    "target_file",
    "target_anchor",
    "source_mapped_block_id",
    "candidate_block_ids",
    "resolved_block_id",
    "resolution_status",
}
_GLOBAL_CANDIDATE_FIELDS = {
    "candidate_id",
    "candidate_kind",
    "source_signal",
    "source_ref",
    "title",
    "unit_ids",
    "block_ids",
    "at_block_id",
    "resolution_status",
    "signals",
    "input_identity_sha256",
}
_GLOBAL_ISSUE_FIELDS = {
    "issue_id",
    "code",
    "scope",
    "target_id",
    "candidate_ids",
    "evidence",
}
_GLOBAL_STATISTICS_FIELDS = {
    "unit_count",
    "block_count",
    "navigation_entry_count",
    "navigation_unresolved_count",
    "navigation_mismatch_ratio",
    "candidate_count",
    "issue_count",
}
_GLOBAL_INTEGRITY_FIELDS = {
    "candidate_count",
    "issue_count",
    "payload_sha256",
}
_PROPOSER_FIELDS = {"kind", "identifier"}
_PLAN_FIELDS = {
    "schema_version",
    "doc_id",
    "report_sha256",
    "inputs",
    "proposer",
    "actions",
    "integrity",
}
_PLAN_ACTION_FIELDS = {
    "action_id",
    "action_type",
    "status",
    "reason",
    "target_unit_ids",
    "before_sha256",
    "parameters",
}
_PLAN_INTEGRITY_FIELDS = {"action_count", "payload_sha256"}

_HIERARCHY_INPUT_FIELDS = {
    "source",
    "document",
    "structure",
    "asset_manifest",
    "admitted_projection",
    "project_state",
    "report",
    "skeleton",
    "policy",
}
_HIERARCHY_PLAN_FIELDS = {
    "schema_version",
    "doc_id",
    "state",
    "inputs",
    "proposer",
    "actions",
    "integrity",
}
_HIERARCHY_PLAN_ACTION_FIELDS = {
    "action_id",
    "action_type",
    "status",
    "reason",
    "child_unit_id",
    "parent_unit_id",
    "before_parent_unit_id",
    "before_sha256",
}
_HIERARCHY_PLAN_INTEGRITY_FIELDS = {"action_count", "payload_sha256"}
_HIERARCHY_OVERLAY_FIELDS = {
    "schema_version",
    "doc_id",
    "state",
    "inputs",
    "plan_sha256",
    "rows",
    "integrity",
}
_HIERARCHY_OVERLAY_ROW_FIELDS = {
    "child_unit_id",
    "order_index",
    "parent_unit_id",
}
_HIERARCHY_OVERLAY_INTEGRITY_FIELDS = {"row_count", "payload_sha256"}

_UPDATE_FIELDS = {"action_type", "unit_id", "new_title", "classification"}
_SPLIT_FIELDS = {
    "action_type",
    "unit_id",
    "at_block_id",
    "left_title",
    "right_title",
    "left_classification",
    "right_classification",
}
_MERGE_FIELDS = {
    "action_type",
    "left_unit_id",
    "right_unit_id",
    "new_title",
    "classification",
}
_HIERARCHY_SET_PARENT_FIELDS = {
    "action_type",
    "child_unit_id",
    "parent_unit_id",
}
_HIERARCHY_CLEAR_PARENT_FIELDS = {"action_type", "child_unit_id"}

_RICH_TRANSLATE_STRUCTURED = frozenset({"table"})
_PRESERVE_SOURCE_KINDS = frozenset(
    {
        "code",
        "directive",
        "equation",
        "image",
        "math",
        "math_block",
        "raw_html",
        "separator",
    }
)


class DraftStructureError(ValueError):
    pass


class DraftStructureFrozenError(DraftStructureError):
    pass


@dataclass(frozen=True)
class GlobalStructurePolicy:
    mechanical_line_max_chars: int = 180
    high_navigation_mismatch_ratio: float = 0.25
    high_navigation_mismatch_min_entries: int = 4
    candidate_overflow_threshold: int = 256
    signal_starvation_min_blocks: int = 8
    text_like_block_types: tuple[str, ...] = (
        "paragraph",
        "dialogue",
        "footnote",
    )

    def validate(self) -> None:
        if not 1 <= self.mechanical_line_max_chars <= 500:
            raise DraftStructureError(
                "global policy mechanical_line_max_chars must be 1..500"
            )
        ratio = self.high_navigation_mismatch_ratio
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise DraftStructureError(
                "global policy high_navigation_mismatch_ratio must be numeric"
            )
        if not 0 <= float(ratio) <= 1:
            raise DraftStructureError(
                "global policy high_navigation_mismatch_ratio must be 0..1"
            )
        for name, value in {
            "high_navigation_mismatch_min_entries": (
                self.high_navigation_mismatch_min_entries
            ),
            "candidate_overflow_threshold": self.candidate_overflow_threshold,
            "signal_starvation_min_blocks": self.signal_starvation_min_blocks,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise DraftStructureError(f"global policy {name} must be positive")
        allowed = {"heading", "paragraph", "dialogue", "footnote"}
        if (
            not self.text_like_block_types
            or len(self.text_like_block_types) != len(set(self.text_like_block_types))
            or any(item not in allowed for item in self.text_like_block_types)
        ):
            raise DraftStructureError(
                "global policy text_like_block_types must be a unique closed subset"
            )

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": GLOBAL_STRUCTURE_POLICY_VERSION,
            "mechanical_line_max_chars": self.mechanical_line_max_chars,
            "high_navigation_mismatch_ratio": float(
                self.high_navigation_mismatch_ratio
            ),
            "high_navigation_mismatch_min_entries": (
                self.high_navigation_mismatch_min_entries
            ),
            "candidate_overflow_threshold": self.candidate_overflow_threshold,
            "signal_starvation_min_blocks": self.signal_starvation_min_blocks,
            "text_like_block_types": list(self.text_like_block_types),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GlobalStructurePolicy":
        if not isinstance(payload, dict):
            raise DraftStructureError("global skeleton policy must be an object")
        expected = {
            "schema_version",
            "mechanical_line_max_chars",
            "high_navigation_mismatch_ratio",
            "high_navigation_mismatch_min_entries",
            "candidate_overflow_threshold",
            "signal_starvation_min_blocks",
            "text_like_block_types",
        }
        _require_exact_fields(payload, expected, owner="global skeleton policy")
        if payload.get("schema_version") != GLOBAL_STRUCTURE_POLICY_VERSION:
            raise DraftStructureError("global skeleton policy version differs")
        types = payload.get("text_like_block_types")
        if not isinstance(types, list):
            raise DraftStructureError(
                "global skeleton policy text_like_block_types must be a list"
            )
        policy = cls(
            mechanical_line_max_chars=payload.get("mechanical_line_max_chars"),
            high_navigation_mismatch_ratio=payload.get(
                "high_navigation_mismatch_ratio"
            ),
            high_navigation_mismatch_min_entries=payload.get(
                "high_navigation_mismatch_min_entries"
            ),
            candidate_overflow_threshold=payload.get(
                "candidate_overflow_threshold"
            ),
            signal_starvation_min_blocks=payload.get(
                "signal_starvation_min_blocks"
            ),
            text_like_block_types=tuple(types),
        )
        policy.validate()
        return policy


class DraftStructureExecutor(Protocol):
    def propose(
        self,
        report: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class DraftStructureResult:
    document: dict[str, Any]
    structure_manifest: dict[str, Any]
    normalization_receipt: dict[str, Any]
    correction_receipt: dict[str, Any]


@dataclass(frozen=True)
class DraftStructureWriteResult:
    output_dir: Path
    document_path: Path
    structure_manifest_path: Path
    admitted_projection_path: Path
    correction_receipt_path: Path


@dataclass
class _WorkingUnit:
    record: dict[str, Any]
    chapter_template: dict[str, Any]
    blocks: list[dict[str, Any]]
    source_unit_ids: tuple[str, ...]
    classification: str


def _require_exact_fields(
    payload: dict[str, Any],
    fields: set[str],
    *,
    owner: str,
) -> None:
    actual = set(payload)
    if actual == fields:
        return
    raise DraftStructureError(
        f"{owner} fields differ; missing={sorted(fields - actual)}, "
        f"extra={sorted(actual - fields)}"
    )


def _require_nonempty_string(value: Any, *, owner: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftStructureError(f"{owner} must be a non-empty string")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise DraftStructureError(f"{owner} contains control characters")
    return value.strip()


def _payload_without_integrity(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key != "integrity"
    }


def _validate_project_state(
    project_state: dict[str, Any],
    *,
    doc_id: str,
) -> None:
    if not isinstance(project_state, dict):
        raise DraftStructureError("project_state must be an object")
    _require_exact_fields(
        project_state,
        _PROJECT_STATE_FIELDS,
        owner="project_state",
    )
    if project_state.get("schema_version") != DRAFT_PROJECT_STATE_VERSION:
        raise DraftStructureError(
            f"project_state.schema_version must be {DRAFT_PROJECT_STATE_VERSION}"
        )
    if project_state.get("doc_id") != doc_id:
        raise DraftStructureError("project_state doc_id differs from document")
    run_count = project_state.get("pipeline_run_count")
    if isinstance(run_count, bool) or not isinstance(run_count, int) or run_count < 0:
        raise DraftStructureError(
            "project_state.pipeline_run_count must be a non-negative integer"
        )
    lifecycle = project_state.get("lifecycle")
    if lifecycle != "draft" or run_count != 0:
        raise DraftStructureFrozenError(
            "structure editing is only allowed for a draft project with no pipeline run"
        )


def _flatten_document(
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list):
        raise DraftStructureError("document.chapters must be a list")
    blocks = [
        block
        for chapter in chapters
        for block in chapter.get("blocks") or []
    ]
    return chapters, blocks


def _validate_base_inputs(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    *,
    package_root: str | Path | None = None,
) -> str:
    source = structure_manifest.get("source")
    if not isinstance(source, dict):
        raise DraftStructureError("structure.source must be an object")
    source_format = _require_nonempty_string(
        source.get("format"),
        owner="structure.source.format",
    )
    try:
        validate_normalization_contract(
            document,
            structure_manifest,
            expected_format=source_format,
        )
        validate_canonical_source_package(
            document,
            structure_manifest,
            asset_manifest,
            package_root=package_root,
        )
        validate_admitted_projection(
            admitted_projection,
            document,
            structure_manifest,
            asset_manifest,
        )
    except ValueError as exc:
        raise DraftStructureError(str(exc)) from exc
    return source_format


def _identity(schema_version: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "sha256": canonical_json_sha256(payload),
    }


def _input_identities(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
) -> dict[str, Any]:
    source = structure_manifest["source"]
    return {
        "source": {
            "schema_version": str(source.get("format") or ""),
            "sha256": str(source.get("sha256") or ""),
        },
        "document": _identity(document.get("schema_version"), document),
        "structure": _identity(
            structure_manifest.get("schema_version"),
            structure_manifest,
        ),
        "asset_manifest": _identity(
            asset_manifest.get("schema_version"),
            asset_manifest,
        ),
        "admitted_projection": _identity(
            admitted_projection.get("schema_version"),
            admitted_projection,
        ),
        "project_state": _identity(
            project_state.get("schema_version"),
            project_state,
        ),
    }


def _classification_for_unit(unit: dict[str, Any]) -> str:
    policy = str(unit.get("translation_policy") or "")
    if unit.get("review_required") is True or policy == "review":
        return "review"
    if policy == "translate":
        return "translate"
    if policy in {"preserve", "exclude"}:
        return policy
    raise DraftStructureError(f"unsupported unit translation policy: {policy}")


def _issue(
    *,
    code: str,
    scope: str,
    target_id: str,
    evidence: list[str],
) -> dict[str, Any]:
    payload = {
        "code": code,
        "scope": scope,
        "target_id": target_id,
        "evidence": evidence,
    }
    return {
        "issue_id": f"amb_{canonical_json_sha256(payload)[:20]}",
        **payload,
    }


_GLOBAL_CANDIDATE_KINDS = frozenset(
    {
        "existing_unit_boundary",
        "internal_heading",
        "mechanical_text_boundary",
        "navigation_entry",
        "duplicate_title_group",
        "numbering_restart",
        "signal_starvation",
    }
)
_GLOBAL_RESOLUTION_STATUSES = frozenset(
    {
        "anchored",
        "source_exact",
        "title_exact_unique",
        "unresolved_zero_match",
        "unresolved_multiple_match",
        "group",
        "document",
    }
)
_GLOBAL_ISSUE_CODES = frozenset(
    {
        "global_structure_internal_heading",
        "global_structure_unresolved_navigation",
        "global_structure_high_navigation_mismatch",
        "global_structure_duplicate_title_group",
        "global_structure_numbering_restart",
        "global_structure_signal_starvation",
        "global_structure_candidate_overflow",
    }
)
_GLOBAL_CHAPTER_MARKER_RE = re.compile(
    r"^(?:chapter|chapitre|cap[ií]tulo|capitolo|kapitel|"
    r"ch(?:ương|uong)|stave|letter|book|part)\b",
    re.IGNORECASE,
)
_GLOBAL_NUMBERED_TITLE_RE = re.compile(
    r"^(chapter|chapitre|cap[ií]tulo|capitolo|kapitel|"
    r"ch(?:ương|uong)|stave|letter|book|part)\s+"
    r"([IVXLCDM]{1,12}|\d{1,4})(?:\b|[. :\-])",
    re.IGNORECASE,
)
_GLOBAL_ROMAN_RE = re.compile(r"^[IVXLCDM]{1,12}$", re.IGNORECASE)
_GLOBAL_ARABIC_RE = re.compile(r"^\d{1,4}$")


def _normalize_structure_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = "".join(character if character.isalnum() else " " for character in text)
    return " ".join(text.split())


def _roman_value(value: str) -> int | None:
    token = value.upper()
    if not _GLOBAL_ROMAN_RE.fullmatch(token):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for character in reversed(token):
        current = values[character]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    if total <= 0:
        return None
    # Reject non-canonical Roman spellings so the detector stays mechanical.
    canonical_pairs = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    remaining = total
    rendered: list[str] = []
    for amount, glyph in canonical_pairs:
        while remaining >= amount:
            rendered.append(glyph)
            remaining -= amount
    return total if "".join(rendered) == token else None


def _numbered_title(value: Any) -> tuple[str, int] | None:
    text = " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
    match = _GLOBAL_NUMBERED_TITLE_RE.match(text)
    if not match:
        return None
    family = _normalize_structure_title(match.group(1))
    ordinal_token = match.group(2)
    if _GLOBAL_ARABIC_RE.fullmatch(ordinal_token):
        ordinal = int(ordinal_token)
    else:
        ordinal = _roman_value(ordinal_token)
    if ordinal is None:
        return None
    return family, ordinal


def _mechanical_structure_signals(
    block: dict[str, Any],
    *,
    policy: GlobalStructurePolicy,
) -> list[str]:
    block_type = str(block.get("block_type") or "")
    if block_type not in policy.text_like_block_types:
        return []
    raw_text = str(block.get("clean_text") or block.get("source_text") or "")
    text = " ".join(raw_text.split())
    if not text or len(text) > policy.mechanical_line_max_chars:
        return []
    signals: list[str] = []
    if _GLOBAL_CHAPTER_MARKER_RE.match(text):
        signals.append("chapter_marker_pattern")
    if _GLOBAL_ROMAN_RE.fullmatch(text):
        signals.append("roman_ordinal_line")
    if _GLOBAL_ARABIC_RE.fullmatch(text):
        signals.append("arabic_ordinal_line")
    letters = [character for character in text if character.isalpha()]
    word_count = len(text.split())
    if (
        letters
        and all(not character.islower() for character in letters)
        and 2 <= word_count <= 16
    ):
        signals.append("uppercase_line")
    return sorted(set(signals))


def _global_candidate(
    *,
    candidate_kind: str,
    source_signal: str,
    source_ref: str,
    title: str | None,
    unit_ids: list[str],
    block_ids: list[str],
    at_block_id: str | None,
    resolution_status: str,
    signals: list[str],
    input_identity_sha256: str,
) -> dict[str, Any]:
    if candidate_kind not in _GLOBAL_CANDIDATE_KINDS:
        raise DraftStructureError("unsupported global candidate kind")
    if resolution_status not in _GLOBAL_RESOLUTION_STATUSES:
        raise DraftStructureError("unsupported global candidate resolution")
    body = {
        "candidate_kind": candidate_kind,
        "source_signal": source_signal,
        "source_ref": source_ref,
        "title": title,
        "unit_ids": list(unit_ids),
        "block_ids": list(block_ids),
        "at_block_id": at_block_id,
        "resolution_status": resolution_status,
        "signals": sorted(set(signals)),
        "input_identity_sha256": input_identity_sha256,
    }
    return {
        "candidate_id": f"cand_{canonical_json_sha256(body)[:24]}",
        **body,
    }


def _global_issue(
    *,
    code: str,
    scope: str,
    target_id: str,
    candidate_ids: list[str],
    evidence: list[str],
) -> dict[str, Any]:
    body = {
        "code": code,
        "scope": scope,
        "target_id": target_id,
        "candidate_ids": sorted(set(candidate_ids)),
        "evidence": sorted(set(evidence)),
    }
    return {
        "issue_id": f"gsi_{canonical_json_sha256(body)[:24]}",
        **body,
    }


def _skeleton_input_identities(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    policy_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "document": _identity(document.get("schema_version"), document),
        "structure": _identity(
            structure_manifest.get("schema_version"), structure_manifest
        ),
        "asset_manifest": _identity(
            asset_manifest.get("schema_version"), asset_manifest
        ),
        "admitted_projection": _identity(
            admitted_projection.get("schema_version"), admitted_projection
        ),
        "policy": _identity(policy_payload.get("schema_version"), policy_payload),
    }


def build_global_structure_skeleton(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    *,
    policy: GlobalStructurePolicy | None = None,
    package_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build a sealed whole-book signal inventory without changing source data."""

    _validate_base_inputs(
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        package_root=package_root,
    )
    active_policy = policy or GlobalStructurePolicy()
    policy_payload = active_policy.to_payload()
    inputs = _skeleton_input_identities(
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        policy_payload,
    )
    input_identity_sha256 = canonical_json_sha256(inputs)
    doc_id = _require_nonempty_string(document.get("doc_id"), owner="document.doc_id")
    chapters, blocks = _flatten_document(document)
    units = structure_manifest.get("units")
    if not isinstance(units, list) or len(units) != len(chapters):
        raise DraftStructureError(
            "structure units must map one-to-one to document chapters"
        )

    by_block: dict[str, dict[str, Any]] = {}
    block_order: dict[str, int] = {}
    block_owner: dict[str, str] = {}
    unit_order: dict[str, int] = {}
    unit_first_block: dict[str, str | None] = {}
    outline: list[dict[str, Any]] = []
    for unit_index, (unit, chapter) in enumerate(zip(units, chapters, strict=True)):
        unit_id = _require_nonempty_string(
            unit.get("unit_id"), owner=f"structure.units[{unit_index}].unit_id"
        )
        chapter_id = _require_nonempty_string(
            chapter.get("chapter_id"),
            owner=f"document.chapters[{unit_index}].chapter_id",
        )
        chapter_blocks = chapter.get("blocks") or []
        block_ids: list[str] = []
        for block in chapter_blocks:
            block_id = _require_nonempty_string(
                block.get("block_id"), owner="document.block.block_id"
            )
            if block_id in by_block:
                raise DraftStructureError("document repeats block_id")
            by_block[block_id] = block
            block_order[block_id] = len(block_order)
            block_owner[block_id] = unit_id
            block_ids.append(block_id)
        unit_order[unit_id] = unit_index
        unit_first_block[unit_id] = block_ids[0] if block_ids else None
        outline.append(
            {
                "unit_id": unit_id,
                "chapter_id": chapter_id,
                "order_index": unit_index,
                "title": str(unit.get("title") or chapter.get("title") or ""),
                "first_block_id": block_ids[0] if block_ids else None,
                "last_block_id": block_ids[-1] if block_ids else None,
                "block_count": len(block_ids),
                "parent_unit_id": unit.get("parent_unit_id"),
            }
        )

    source_file_by_block: dict[str, str] = {}
    source_map = structure_manifest.get("source_map")
    if isinstance(source_map, list):
        for row in source_map:
            if not isinstance(row, dict):
                continue
            block_id = str(row.get("block_id") or "")
            if block_id not in by_block:
                continue
            source_file = str(
                row.get("epub_file")
                or row.get("source_file")
                or row.get("html_file")
                or ""
            )
            if source_file:
                source_file_by_block[block_id] = source_file

    normalized_blocks: dict[str, list[str]] = {}
    for block_id, block in by_block.items():
        normalized = _normalize_structure_title(
            block.get("clean_text") or block.get("source_text") or ""
        )
        if normalized:
            normalized_blocks.setdefault(normalized, []).append(block_id)

    navigation_source = structure_manifest.get("package_structure")
    raw_navigation = (
        navigation_source.get("navigation")
        if isinstance(navigation_source, dict)
        else []
    )
    if not isinstance(raw_navigation, list):
        raw_navigation = []
    navigation: list[dict[str, Any]] = []
    seen_entry_ids: set[str] = set()
    for index, entry in enumerate(raw_navigation):
        if not isinstance(entry, dict):
            raise DraftStructureError("structure navigation entry must be an object")
        entry_id = _require_nonempty_string(
            entry.get("entry_id"), owner=f"navigation[{index}].entry_id"
        )
        if entry_id in seen_entry_ids:
            raise DraftStructureError("structure navigation repeats entry_id")
        seen_entry_ids.add(entry_id)
        title = str(entry.get("title") or "")
        normalized_title = _normalize_structure_title(title)
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        target_file = str(target.get("file") or "") or None
        target_anchor = str(target.get("anchor") or "") or None
        mapped = entry.get("mapped_block")
        source_mapped_block_id: str | None = None
        if isinstance(mapped, int) and not isinstance(mapped, bool) and 0 <= mapped < len(blocks):
            source_mapped_block_id = str(blocks[mapped]["block_id"])

        if source_mapped_block_id is not None:
            matches = [source_mapped_block_id]
            resolved_block_id = source_mapped_block_id
            status = "source_exact"
        else:
            scope = list(by_block)
            if target_file:
                scoped = [
                    block_id
                    for block_id in scope
                    if source_file_by_block.get(block_id) == target_file
                ]
                scope = scoped
            allowed = set(scope)
            matches = [
                block_id
                for block_id in normalized_blocks.get(normalized_title, [])
                if block_id in allowed
            ]
            matches.sort(key=block_order.__getitem__)
            if len(matches) == 1:
                resolved_block_id = matches[0]
                status = "title_exact_unique"
            elif matches:
                resolved_block_id = None
                status = "unresolved_multiple_match"
            else:
                resolved_block_id = None
                status = "unresolved_zero_match"
        navigation.append(
            {
                "entry_id": entry_id,
                "order_index": index,
                "title": title,
                "normalized_title": normalized_title,
                "depth": entry.get("depth"),
                "parent_id": entry.get("parent_id"),
                "target_file": target_file,
                "target_anchor": target_anchor,
                "source_mapped_block_id": source_mapped_block_id,
                "candidate_block_ids": matches,
                "resolved_block_id": resolved_block_id,
                "resolution_status": status,
            }
        )

    candidates: list[dict[str, Any]] = []

    def add_candidate(**kwargs: Any) -> dict[str, Any]:
        candidate = _global_candidate(
            input_identity_sha256=input_identity_sha256,
            **kwargs,
        )
        candidates.append(candidate)
        return candidate

    for index, unit in enumerate(outline[1:], start=1):
        left = outline[index - 1]
        block_id = unit["first_block_id"]
        add_candidate(
            candidate_kind="existing_unit_boundary",
            source_signal="canonical_unit_boundary",
            source_ref=f"unit:{unit['unit_id']}",
            title=None,
            unit_ids=[left["unit_id"], unit["unit_id"]],
            block_ids=[block_id] if block_id else [],
            at_block_id=block_id,
            resolution_status="anchored",
            signals=["existing_unit_boundary"],
        )

    for unit in outline:
        unit_id = unit["unit_id"]
        chapter = chapters[unit_order[unit_id]]
        chapter_blocks = chapter.get("blocks") or []
        for block in chapter_blocks[1:]:
            block_id = str(block["block_id"])
            if block.get("block_type") == "heading":
                add_candidate(
                    candidate_kind="internal_heading",
                    source_signal="runtime_heading_inside_unit",
                    source_ref=f"block:{block_id}",
                    title=None,
                    unit_ids=[unit_id],
                    block_ids=[block_id],
                    at_block_id=block_id,
                    resolution_status="anchored",
                    signals=["internal_heading"],
                )
                continue
            signals = _mechanical_structure_signals(
                block,
                policy=active_policy,
            )
            if signals:
                add_candidate(
                    candidate_kind="mechanical_text_boundary",
                    source_signal="mechanical_text_scan",
                    source_ref=f"block:{block_id}",
                    title=None,
                    unit_ids=[unit_id],
                    block_ids=[block_id],
                    at_block_id=block_id,
                    resolution_status="anchored",
                    signals=signals,
                )

    nav_candidate_by_entry: dict[str, dict[str, Any]] = {}
    for entry in navigation:
        block_ids = list(entry["candidate_block_ids"])
        unit_ids = sorted(
            {block_owner[block_id] for block_id in block_ids},
            key=unit_order.__getitem__,
        )
        candidate = add_candidate(
            candidate_kind="navigation_entry",
            source_signal="navigation_or_toc",
            source_ref=f"navigation:{entry['entry_id']}",
            title=entry["title"],
            unit_ids=unit_ids,
            block_ids=block_ids,
            at_block_id=entry["resolved_block_id"],
            resolution_status=entry["resolution_status"],
            signals=[
                "navigation_entry",
                f"navigation_depth:{entry['depth']}",
                entry["resolution_status"],
            ],
        )
        nav_candidate_by_entry[entry["entry_id"]] = candidate

    title_groups: dict[str, dict[str, set[str]]] = {}
    for unit in outline:
        normalized = _normalize_structure_title(unit["title"])
        if normalized:
            group = title_groups.setdefault(
                normalized, {"unit_ids": set(), "entry_ids": set()}
            )
            group["unit_ids"].add(unit["unit_id"])
    for entry in navigation:
        normalized = entry["normalized_title"]
        if normalized:
            group = title_groups.setdefault(
                normalized, {"unit_ids": set(), "entry_ids": set()}
            )
            group["entry_ids"].add(entry["entry_id"])
    duplicate_candidates: list[dict[str, Any]] = []
    for normalized, group in sorted(title_groups.items()):
        if len(group["unit_ids"]) <= 1 and len(group["entry_ids"]) <= 1:
            continue
        group_unit_ids = sorted(group["unit_ids"], key=unit_order.__getitem__)
        group_block_ids: set[str] = {
            block_id
            for unit_id in group_unit_ids
            if (block_id := unit_first_block.get(unit_id)) is not None
        }
        for entry_id in group["entry_ids"]:
            group_block_ids.update(
                nav_candidate_by_entry[entry_id]["block_ids"]
            )
        duplicate_candidates.append(
            add_candidate(
                candidate_kind="duplicate_title_group",
                source_signal="normalized_title_collision",
                source_ref=f"title:{normalized}",
                title=normalized,
                unit_ids=group_unit_ids,
                block_ids=sorted(group_block_ids, key=block_order.__getitem__),
                at_block_id=None,
                resolution_status="group",
                signals=[
                    "duplicate_normalized_title",
                    f"unit_count:{len(group_unit_ids)}",
                    f"navigation_count:{len(group['entry_ids'])}",
                ],
            )
        )

    restart_candidates: list[dict[str, Any]] = []

    def add_restarts(
        rows: list[tuple[str, str, str | None, list[str], list[str]]],
        *,
        source: str,
    ) -> None:
        previous: dict[str, tuple[int, str]] = {}
        for source_id, title, at_block_id, row_unit_ids, row_block_ids in rows:
            numbered = _numbered_title(title)
            if numbered is None:
                continue
            family, ordinal = numbered
            prior = previous.get(family)
            if prior is not None and ordinal <= prior[0]:
                restart_candidates.append(
                    add_candidate(
                        candidate_kind="numbering_restart",
                        source_signal=f"{source}_numbering_sequence",
                        source_ref=f"{source}:{source_id}",
                        title=title,
                        unit_ids=row_unit_ids,
                        block_ids=row_block_ids,
                        at_block_id=at_block_id,
                        resolution_status=(
                            "anchored" if at_block_id is not None else "group"
                        ),
                        signals=[
                            f"number_family:{family}",
                            f"previous_ordinal:{prior[0]}",
                            f"current_ordinal:{ordinal}",
                            f"previous_source_ref:{prior[1]}",
                        ],
                    )
                )
            previous[family] = (ordinal, source_id)

    add_restarts(
        [
            (
                unit["unit_id"],
                unit["title"],
                unit["first_block_id"],
                [unit["unit_id"]],
                [unit["first_block_id"]] if unit["first_block_id"] else [],
            )
            for unit in outline
        ],
        source="unit",
    )
    add_restarts(
        [
            (
                entry["entry_id"],
                entry["title"],
                entry["resolved_block_id"],
                sorted(
                    {
                        block_owner[block_id]
                        for block_id in entry["candidate_block_ids"]
                    },
                    key=unit_order.__getitem__,
                ),
                list(entry["candidate_block_ids"]),
            )
            for entry in navigation
        ],
        source="navigation",
    )

    starvation_candidate: dict[str, Any] | None = None
    structural_candidates = [
        row
        for row in candidates
        if row["candidate_kind"]
        in {
            "internal_heading",
            "mechanical_text_boundary",
            "navigation_entry",
        }
        and (
            row["candidate_kind"] != "navigation_entry"
            or row["resolution_status"]
            in {"source_exact", "title_exact_unique", "unresolved_multiple_match"}
        )
    ]
    if (
        len(outline) == 1
        and len(blocks) >= active_policy.signal_starvation_min_blocks
        and not structural_candidates
    ):
        starvation_candidate = add_candidate(
            candidate_kind="signal_starvation",
            source_signal="whole_book_signal_scan",
            source_ref=f"document:{doc_id}",
            title=None,
            unit_ids=[outline[0]["unit_id"]] if outline else [],
            block_ids=[],
            at_block_id=None,
            resolution_status="document",
            signals=[
                "single_unit",
                f"block_count:{len(blocks)}",
                "no_usable_boundary_signal",
            ],
        )

    candidates.sort(
        key=lambda row: (
            min(
                (block_order[block_id] for block_id in row["block_ids"]),
                default=len(blocks),
            ),
            row["candidate_kind"],
            row["source_ref"],
            row["candidate_id"],
        )
    )

    issues: list[dict[str, Any]] = []
    internal_by_unit: dict[str, list[str]] = {}
    for candidate in candidates:
        if candidate["candidate_kind"] == "internal_heading":
            internal_by_unit.setdefault(candidate["unit_ids"][0], []).append(
                candidate["candidate_id"]
            )
    for unit_id, candidate_ids in sorted(
        internal_by_unit.items(), key=lambda item: unit_order[item[0]]
    ):
        issues.append(
            _global_issue(
                code="global_structure_internal_heading",
                scope="unit",
                target_id=unit_id,
                candidate_ids=candidate_ids,
                evidence=[f"internal_heading_count:{len(candidate_ids)}"],
            )
        )

    unresolved_navigation = [
        entry
        for entry in navigation
        if entry["resolution_status"]
        in {"unresolved_zero_match", "unresolved_multiple_match"}
    ]
    for entry in unresolved_navigation:
        candidate = nav_candidate_by_entry[entry["entry_id"]]
        issues.append(
            _global_issue(
                code="global_structure_unresolved_navigation",
                scope="document",
                target_id=doc_id,
                candidate_ids=[candidate["candidate_id"]],
                evidence=[
                    f"entry_id:{entry['entry_id']}",
                    f"resolution:{entry['resolution_status']}",
                    f"candidate_block_count:{len(entry['candidate_block_ids'])}",
                ],
            )
        )
    mismatch_ratio = (
        len(unresolved_navigation) / len(navigation) if navigation else 0.0
    )
    if (
        len(navigation) >= active_policy.high_navigation_mismatch_min_entries
        and mismatch_ratio >= active_policy.high_navigation_mismatch_ratio
    ):
        issues.append(
            _global_issue(
                code="global_structure_high_navigation_mismatch",
                scope="document",
                target_id=doc_id,
                candidate_ids=[
                    nav_candidate_by_entry[entry["entry_id"]]["candidate_id"]
                    for entry in unresolved_navigation
                ],
                evidence=[
                    f"entry_count:{len(navigation)}",
                    f"unresolved_count:{len(unresolved_navigation)}",
                    f"mismatch_ratio:{mismatch_ratio:.6f}",
                    "threshold:"
                    f"{active_policy.high_navigation_mismatch_ratio:.6f}",
                ],
            )
        )
    for candidate in duplicate_candidates:
        issues.append(
            _global_issue(
                code="global_structure_duplicate_title_group",
                scope="document",
                target_id=doc_id,
                candidate_ids=[candidate["candidate_id"]],
                evidence=[candidate["source_ref"], *candidate["signals"]],
            )
        )
    for candidate in restart_candidates:
        target_id = candidate["unit_ids"][0] if candidate["unit_ids"] else doc_id
        issues.append(
            _global_issue(
                code="global_structure_numbering_restart",
                scope="unit" if candidate["unit_ids"] else "document",
                target_id=target_id,
                candidate_ids=[candidate["candidate_id"]],
                evidence=[candidate["source_ref"], *candidate["signals"]],
            )
        )
    if starvation_candidate is not None:
        issues.append(
            _global_issue(
                code="global_structure_signal_starvation",
                scope="document",
                target_id=doc_id,
                candidate_ids=[starvation_candidate["candidate_id"]],
                evidence=list(starvation_candidate["signals"]),
            )
        )
    if len(candidates) > active_policy.candidate_overflow_threshold:
        issues.append(
            _global_issue(
                code="global_structure_candidate_overflow",
                scope="document",
                target_id=doc_id,
                candidate_ids=[row["candidate_id"] for row in candidates],
                evidence=[
                    f"candidate_count:{len(candidates)}",
                    f"threshold:{active_policy.candidate_overflow_threshold}",
                    "inventory_preserved:true",
                ],
            )
        )
    issues.sort(
        key=lambda row: (
            row["scope"],
            row["target_id"],
            row["code"],
            row["issue_id"],
        )
    )

    payload: dict[str, Any] = {
        "schema_version": GLOBAL_STRUCTURE_SKELETON_VERSION,
        "doc_id": doc_id,
        "inputs": inputs,
        "policy": policy_payload,
        "outline": outline,
        "navigation": navigation,
        "candidates": candidates,
        "issues": issues,
        "statistics": {
            "unit_count": len(outline),
            "block_count": len(blocks),
            "navigation_entry_count": len(navigation),
            "navigation_unresolved_count": len(unresolved_navigation),
            "navigation_mismatch_ratio": round(mismatch_ratio, 12),
            "candidate_count": len(candidates),
            "issue_count": len(issues),
        },
    }
    payload["integrity"] = {
        "candidate_count": len(candidates),
        "issue_count": len(issues),
        "payload_sha256": canonical_json_sha256(payload),
    }
    validate_global_structure_skeleton(payload)
    return payload


def validate_global_structure_skeleton(
    skeleton: dict[str, Any],
    *,
    expected_inputs: dict[str, Any] | None = None,
    authoritative_document: dict[str, Any] | None = None,
    authoritative_structure_manifest: dict[str, Any] | None = None,
    authoritative_asset_manifest: dict[str, Any] | None = None,
    authoritative_admitted_projection: dict[str, Any] | None = None,
    package_root: str | Path | None = None,
) -> None:
    if not isinstance(skeleton, dict):
        raise DraftStructureError("global structure skeleton must be an object")
    _require_exact_fields(
        skeleton, _GLOBAL_SKELETON_FIELDS, owner="global structure skeleton"
    )
    if skeleton.get("schema_version") != GLOBAL_STRUCTURE_SKELETON_VERSION:
        raise DraftStructureError("global structure skeleton version differs")
    _require_nonempty_string(skeleton.get("doc_id"), owner="skeleton.doc_id")
    inputs = skeleton.get("inputs")
    if not isinstance(inputs, dict):
        raise DraftStructureError("skeleton.inputs must be an object")
    _require_exact_fields(
        inputs, _GLOBAL_SKELETON_INPUT_FIELDS, owner="skeleton.inputs"
    )
    for name, identity in inputs.items():
        if not isinstance(identity, dict):
            raise DraftStructureError(f"skeleton.inputs.{name} must be an object")
        _require_exact_fields(identity, _IDENTITY_FIELDS, owner=f"skeleton.inputs.{name}")
    if expected_inputs is not None and inputs != expected_inputs:
        raise DraftStructureError("global skeleton input identities differ")
    policy = GlobalStructurePolicy.from_payload(skeleton.get("policy"))
    if inputs["policy"] != _identity(
        GLOBAL_STRUCTURE_POLICY_VERSION, policy.to_payload()
    ):
        raise DraftStructureError("global skeleton policy identity differs")
    input_identity_sha256 = canonical_json_sha256(inputs)

    outline = skeleton.get("outline")
    navigation = skeleton.get("navigation")
    candidates = skeleton.get("candidates")
    issues = skeleton.get("issues")
    statistics = skeleton.get("statistics")
    integrity = skeleton.get("integrity")
    for name, rows in {
        "outline": outline,
        "navigation": navigation,
        "candidates": candidates,
        "issues": issues,
    }.items():
        if not isinstance(rows, list):
            raise DraftStructureError(f"skeleton.{name} must be a list")
    if not isinstance(statistics, dict) or not isinstance(integrity, dict):
        raise DraftStructureError("skeleton statistics/integrity must be objects")
    _require_exact_fields(
        statistics, _GLOBAL_STATISTICS_FIELDS, owner="skeleton.statistics"
    )
    _require_exact_fields(integrity, _GLOBAL_INTEGRITY_FIELDS, owner="skeleton.integrity")

    unit_ids: list[str] = []
    block_order: dict[str, int] = {}
    for index, row in enumerate(outline):
        if not isinstance(row, dict):
            raise DraftStructureError(f"skeleton.outline[{index}] must be an object")
        _require_exact_fields(row, _GLOBAL_OUTLINE_FIELDS, owner=f"skeleton.outline[{index}]")
        unit_id = _require_nonempty_string(row.get("unit_id"), owner="outline.unit_id")
        unit_ids.append(unit_id)
        if row.get("order_index") != index:
            raise DraftStructureError("skeleton outline order differs")
        first = row.get("first_block_id")
        last = row.get("last_block_id")
        if first is not None:
            block_order[str(first)] = index
        if last is not None:
            block_order.setdefault(str(last), index)
    if len(unit_ids) != len(set(unit_ids)):
        raise DraftStructureError("skeleton outline repeats unit_id")

    entry_ids: list[str] = []
    for index, row in enumerate(navigation):
        if not isinstance(row, dict):
            raise DraftStructureError(f"skeleton.navigation[{index}] must be an object")
        _require_exact_fields(
            row, _GLOBAL_NAVIGATION_FIELDS, owner=f"skeleton.navigation[{index}]"
        )
        entry_id = _require_nonempty_string(row.get("entry_id"), owner="navigation.entry_id")
        entry_ids.append(entry_id)
        if row.get("order_index") != index:
            raise DraftStructureError("skeleton navigation order differs")
        if row.get("resolution_status") not in {
            "source_exact",
            "title_exact_unique",
            "unresolved_zero_match",
            "unresolved_multiple_match",
        }:
            raise DraftStructureError("skeleton navigation resolution differs")
        candidate_block_ids = row.get("candidate_block_ids")
        if (
            not isinstance(candidate_block_ids, list)
            or len(candidate_block_ids) != len(set(candidate_block_ids))
            or any(not isinstance(item, str) or not item for item in candidate_block_ids)
        ):
            raise DraftStructureError(
                "skeleton navigation candidate_block_ids differ"
            )
        status = row["resolution_status"]
        resolved_block_id = row.get("resolved_block_id")
        if status in {"source_exact", "title_exact_unique"}:
            if (
                not isinstance(resolved_block_id, str)
                or candidate_block_ids != [resolved_block_id]
            ):
                raise DraftStructureError(
                    "resolved navigation must identify exactly one block"
                )
        elif status == "unresolved_zero_match":
            if candidate_block_ids or resolved_block_id is not None:
                raise DraftStructureError(
                    "zero-match navigation must remain explicitly unresolved"
                )
        elif len(candidate_block_ids) < 2 or resolved_block_id is not None:
            raise DraftStructureError(
                "multi-match navigation must retain every candidate block"
            )
    if len(entry_ids) != len(set(entry_ids)):
        raise DraftStructureError("skeleton navigation repeats entry_id")

    candidate_ids: list[str] = []
    for index, row in enumerate(candidates):
        if not isinstance(row, dict):
            raise DraftStructureError(f"skeleton.candidates[{index}] must be an object")
        _require_exact_fields(
            row, _GLOBAL_CANDIDATE_FIELDS, owner=f"skeleton.candidates[{index}]"
        )
        candidate_id = _require_nonempty_string(
            row.get("candidate_id"), owner="candidate.candidate_id"
        )
        candidate_ids.append(candidate_id)
        if row.get("candidate_kind") not in _GLOBAL_CANDIDATE_KINDS:
            raise DraftStructureError("skeleton candidate kind differs")
        if row.get("resolution_status") not in _GLOBAL_RESOLUTION_STATUSES:
            raise DraftStructureError("skeleton candidate resolution differs")
        if row.get("input_identity_sha256") != input_identity_sha256:
            raise DraftStructureError("skeleton candidate input identity differs")
        row_unit_ids = row.get("unit_ids")
        row_block_ids = row.get("block_ids")
        row_signals = row.get("signals")
        if (
            not isinstance(row_unit_ids, list)
            or len(row_unit_ids) != len(set(row_unit_ids))
            or any(item not in set(unit_ids) for item in row_unit_ids)
        ):
            raise DraftStructureError("skeleton candidate unit_ids differ")
        if (
            not isinstance(row_block_ids, list)
            or len(row_block_ids) != len(set(row_block_ids))
            or any(not isinstance(item, str) or not item for item in row_block_ids)
        ):
            raise DraftStructureError("skeleton candidate block_ids differ")
        if row.get("at_block_id") is not None and row["at_block_id"] not in row_block_ids:
            raise DraftStructureError("skeleton candidate boundary differs")
        if (
            not isinstance(row_signals, list)
            or row_signals != sorted(set(row_signals))
            or any(not isinstance(item, str) or not item for item in row_signals)
        ):
            raise DraftStructureError("skeleton candidate signals differ")
        body = {key: copy.deepcopy(value) for key, value in row.items() if key != "candidate_id"}
        if candidate_id != f"cand_{canonical_json_sha256(body)[:24]}":
            raise DraftStructureError("skeleton candidate ID differs")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise DraftStructureError("skeleton repeats candidate_id")
    candidate_id_set = set(candidate_ids)

    issue_ids: list[str] = []
    for index, row in enumerate(issues):
        if not isinstance(row, dict):
            raise DraftStructureError(f"skeleton.issues[{index}] must be an object")
        _require_exact_fields(row, _GLOBAL_ISSUE_FIELDS, owner=f"skeleton.issues[{index}]")
        issue_id = _require_nonempty_string(
            row.get("issue_id"), owner="skeleton issue_id"
        )
        issue_ids.append(issue_id)
        if row.get("code") not in _GLOBAL_ISSUE_CODES:
            raise DraftStructureError("skeleton issue code differs")
        if row.get("scope") == "unit":
            if row.get("target_id") not in set(unit_ids):
                raise DraftStructureError("skeleton unit issue target differs")
        elif row.get("scope") == "document":
            if row.get("target_id") != skeleton["doc_id"]:
                raise DraftStructureError("skeleton document issue target differs")
        else:
            raise DraftStructureError("skeleton issue scope differs")
        if any(item not in candidate_id_set for item in row["candidate_ids"]):
            raise DraftStructureError("skeleton issue references unknown candidate")
        body = {key: copy.deepcopy(value) for key, value in row.items() if key != "issue_id"}
        if row.get("issue_id") != f"gsi_{canonical_json_sha256(body)[:24]}":
            raise DraftStructureError("skeleton issue ID differs")
    if len(issue_ids) != len(set(issue_ids)):
        raise DraftStructureError("skeleton repeats issue_id")

    if statistics.get("unit_count") != len(outline):
        raise DraftStructureError("skeleton unit_count differs")
    if statistics.get("block_count") != sum(row["block_count"] for row in outline):
        raise DraftStructureError("skeleton block_count differs")
    if statistics.get("navigation_entry_count") != len(navigation):
        raise DraftStructureError("skeleton navigation count differs")
    if statistics.get("candidate_count") != len(candidates):
        raise DraftStructureError("skeleton candidate count differs")
    if statistics.get("issue_count") != len(issues):
        raise DraftStructureError("skeleton issue count differs")
    unresolved = sum(
        row["resolution_status"]
        in {"unresolved_zero_match", "unresolved_multiple_match"}
        for row in navigation
    )
    if statistics.get("navigation_unresolved_count") != unresolved:
        raise DraftStructureError("skeleton unresolved navigation count differs")
    ratio = unresolved / len(navigation) if navigation else 0.0
    if statistics.get("navigation_mismatch_ratio") != round(ratio, 12):
        raise DraftStructureError("skeleton navigation mismatch ratio differs")
    if integrity.get("candidate_count") != len(candidates):
        raise DraftStructureError("skeleton integrity candidate_count differs")
    if integrity.get("issue_count") != len(issues):
        raise DraftStructureError("skeleton integrity issue_count differs")
    if integrity.get("payload_sha256") != canonical_json_sha256(
        _payload_without_integrity(skeleton)
    ):
        raise DraftStructureError("global skeleton payload hash differs")

    authoritative_inputs = (
        authoritative_document,
        authoritative_structure_manifest,
        authoritative_asset_manifest,
        authoritative_admitted_projection,
    )
    if any(value is not None for value in authoritative_inputs):
        if any(value is None for value in authoritative_inputs):
            raise DraftStructureError(
                "authoritative global skeleton validation requires the complete "
                "source package"
            )
        expected_skeleton = build_global_structure_skeleton(
            authoritative_document,
            authoritative_structure_manifest,
            authoritative_asset_manifest,
            authoritative_admitted_projection,
            policy=policy,
            package_root=package_root,
        )
        if skeleton != expected_skeleton:
            raise DraftStructureError(
                "global skeleton semantic lineage differs from source package"
            )


def build_draft_structure_report(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    *,
    package_root: str | Path | None = None,
    global_policy: GlobalStructurePolicy | None = None,
) -> dict[str, Any]:
    source_format = _validate_base_inputs(
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        package_root=package_root,
    )
    doc_id = _require_nonempty_string(
        document.get("doc_id"),
        owner="document.doc_id",
    )
    _validate_project_state(project_state, doc_id=doc_id)
    chapters, _blocks = _flatten_document(document)
    units = structure_manifest.get("units")
    bindings = asset_manifest.get("block_bindings")
    rows = admitted_projection.get("rows")
    if not isinstance(units, list) or len(units) != len(chapters):
        raise DraftStructureError(
            "structure units must map one-to-one to document chapters"
        )
    if not isinstance(bindings, list) or not isinstance(rows, list):
        raise DraftStructureError("asset bindings and projection rows must be lists")

    global_skeleton = build_global_structure_skeleton(
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        policy=global_policy,
        package_root=package_root,
    )
    global_unit_issues: dict[str, list[dict[str, Any]]] = {}
    for global_issue in global_skeleton["issues"]:
        if global_issue["scope"] == "unit":
            global_unit_issues.setdefault(global_issue["target_id"], []).append(
                global_issue
            )

    review_blocks = {
        str(binding.get("block_id") or "")
        for binding in bindings
        if isinstance(binding, dict) and binding.get("review_required") is True
    }
    review_blocks.update(
        str(row.get("block_id") or "")
        for row in rows
        if isinstance(row, dict) and row.get("channel") == "review_required"
    )

    report_units: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for unit, chapter in zip(units, chapters, strict=True):
        unit_id = _require_nonempty_string(
            unit.get("unit_id"),
            owner="structure.unit.unit_id",
        )
        chapter_id = _require_nonempty_string(
            chapter.get("chapter_id"),
            owner="document.chapter.chapter_id",
        )
        block_ids = [
            _require_nonempty_string(
                block.get("block_id"),
                owner="document.block.block_id",
            )
            for block in chapter.get("blocks") or []
        ]
        local_codes: list[str] = []
        if unit.get("review_required") is True:
            local_codes.append("unit_flagged_review")
        if unit.get("role") == "unknown":
            local_codes.append("unit_unknown_role")
        confidence = unit.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise DraftStructureError("unit confidence must be numeric")
        if float(confidence) < 0.8:
            local_codes.append("unit_low_confidence")
        if any(block_id in review_blocks for block_id in block_ids):
            local_codes.append("block_requires_review")
        local_codes = sorted(set(local_codes))
        for code in local_codes:
            issues.append(
                _issue(
                    code=code,
                    scope="unit",
                    target_id=unit_id,
                    evidence=[
                        f"source_format:{source_format}",
                        f"chapter_id:{chapter_id}",
                    ],
                )
            )
        global_codes = [
            issue["code"] for issue in global_unit_issues.get(unit_id, [])
        ]
        codes = sorted(set([*local_codes, *global_codes]))
        report_units.append(
            {
                "unit_id": unit_id,
                "chapter_id": chapter_id,
                "order_index": unit.get("order_index"),
                "title": str(unit.get("title") or ""),
                "block_ids": block_ids,
                "role": str(unit.get("role") or ""),
                "translation_policy": str(
                    unit.get("translation_policy") or ""
                ),
                "confidence": float(confidence),
                "review_required": bool(unit.get("review_required")),
                "issue_codes": codes,
            }
        )
    for global_issue in global_skeleton["issues"]:
        evidence = [
            *global_issue["evidence"],
            *(f"candidate_id:{item}" for item in global_issue["candidate_ids"]),
            f"global_issue_id:{global_issue['issue_id']}",
        ]
        issues.append(
            _issue(
                code=global_issue["code"],
                scope=global_issue["scope"],
                target_id=global_issue["target_id"],
                evidence=evidence,
            )
        )

    warnings = structure_manifest.get("warnings")
    if isinstance(warnings, list):
        for warning in sorted({str(item) for item in warnings if str(item)}):
            issues.append(
                _issue(
                    code="normalizer_warning",
                    scope="document",
                    target_id=doc_id,
                    evidence=[warning],
                )
            )
    issues.sort(
        key=lambda row: (
            row["scope"],
            row["target_id"],
            row["code"],
            row["issue_id"],
        )
    )
    report: dict[str, Any] = {
        "schema_version": DRAFT_STRUCTURE_REPORT_VERSION,
        "doc_id": doc_id,
        "editable": True,
        "inputs": _input_identities(
            document,
            structure_manifest,
            asset_manifest,
            admitted_projection,
            project_state,
        ),
        "units": report_units,
        "issues": issues,
        "global_skeleton": global_skeleton,
    }
    report["integrity"] = {
        "unit_count": len(report_units),
        "issue_count": len(issues),
        "payload_sha256": canonical_json_sha256(report),
    }
    return report


def _validate_report_shape(report: dict[str, Any]) -> None:
    if not isinstance(report, dict):
        raise DraftStructureError("draft structure report must be an object")
    _require_exact_fields(report, _REPORT_FIELDS, owner="report")
    if report.get("schema_version") != DRAFT_STRUCTURE_REPORT_VERSION:
        raise DraftStructureError(
            f"report.schema_version must be {DRAFT_STRUCTURE_REPORT_VERSION}"
        )
    if report.get("editable") is not True:
        raise DraftStructureFrozenError("draft structure report is not editable")
    inputs = report.get("inputs")
    if not isinstance(inputs, dict):
        raise DraftStructureError("report.inputs must be an object")
    _require_exact_fields(inputs, _REPORT_INPUT_FIELDS, owner="report.inputs")
    for name, identity in inputs.items():
        if not isinstance(identity, dict):
            raise DraftStructureError(f"report.inputs.{name} must be an object")
        _require_exact_fields(
            identity,
            _IDENTITY_FIELDS,
            owner=f"report.inputs.{name}",
        )
    global_skeleton = report.get("global_skeleton")
    if not isinstance(global_skeleton, dict):
        raise DraftStructureError("report.global_skeleton must be an object")
    expected_skeleton_inputs = {
        "document": inputs["document"],
        "structure": inputs["structure"],
        "asset_manifest": inputs["asset_manifest"],
        "admitted_projection": inputs["admitted_projection"],
        "policy": global_skeleton.get("inputs", {}).get("policy"),
    }
    validate_global_structure_skeleton(
        global_skeleton,
        expected_inputs=expected_skeleton_inputs,
    )
    if global_skeleton.get("doc_id") != report.get("doc_id"):
        raise DraftStructureError("report and global skeleton doc_id differ")
    units = report.get("units")
    if not isinstance(units, list):
        raise DraftStructureError("report.units must be a list")
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            raise DraftStructureError(f"report.units[{index}] must be an object")
        _require_exact_fields(
            unit,
            _REPORT_UNIT_FIELDS,
            owner=f"report.units[{index}]",
        )
    issues = report.get("issues")
    if not isinstance(issues, list):
        raise DraftStructureError("report.issues must be a list")
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            raise DraftStructureError(f"report.issues[{index}] must be an object")
        _require_exact_fields(
            issue,
            _ISSUE_FIELDS,
            owner=f"report.issues[{index}]",
        )
    integrity = report.get("integrity")
    if not isinstance(integrity, dict):
        raise DraftStructureError("report.integrity must be an object")
    _require_exact_fields(
        integrity,
        _REPORT_INTEGRITY_FIELDS,
        owner="report.integrity",
    )
    if integrity.get("unit_count") != len(units):
        raise DraftStructureError("report unit_count differs")
    if integrity.get("issue_count") != len(issues):
        raise DraftStructureError("report issue_count differs")
    if integrity.get("payload_sha256") != canonical_json_sha256(
        _payload_without_integrity(report)
    ):
        raise DraftStructureError("report payload hash differs")


def validate_draft_structure_report_shape(report: dict[str, Any]) -> None:
    """Validate the complete report contract before advisory model transport."""

    _validate_report_shape(report)


def _validate_title(value: Any, *, owner: str) -> str:
    title = _require_nonempty_string(value, owner=owner)
    if len(title) > 500:
        raise DraftStructureError(f"{owner} is too long")
    return title


def _validate_classification(value: Any, *, owner: str) -> str:
    if value not in CLASSIFICATIONS:
        raise DraftStructureError(
            f"{owner} must be one of {list(CLASSIFICATIONS)}"
        )
    return str(value)


def _unit_before_sha256(
    unit_ids: list[str],
    report_by_id: dict[str, dict[str, Any]],
) -> str | None:
    if not unit_ids or any(unit_id not in report_by_id for unit_id in unit_ids):
        return None
    return canonical_json_sha256([report_by_id[unit_id] for unit_id in unit_ids])


def _action_targets(
    action_type: str,
    parameters: dict[str, Any],
) -> list[str]:
    if action_type in {"update_unit", "split_unit"}:
        return [str(parameters.get("unit_id") or "")]
    return [
        str(parameters.get("left_unit_id") or ""),
        str(parameters.get("right_unit_id") or ""),
    ]


def _validate_action_spec(spec: dict[str, Any], *, index: int) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise DraftStructureError(f"action_specs[{index}] must be an object")
    action_type = spec.get("action_type")
    if action_type == "update_unit":
        _require_exact_fields(spec, _UPDATE_FIELDS, owner=f"action_specs[{index}]")
        unit_id = _require_nonempty_string(
            spec.get("unit_id"),
            owner=f"action_specs[{index}].unit_id",
        )
        new_title = spec.get("new_title")
        classification = spec.get("classification")
        if new_title is not None:
            new_title = _validate_title(
                new_title,
                owner=f"action_specs[{index}].new_title",
            )
        if classification is not None:
            classification = _validate_classification(
                classification,
                owner=f"action_specs[{index}].classification",
            )
        if new_title is None and classification is None:
            raise DraftStructureError(
                f"action_specs[{index}] must change title or classification"
            )
        return {
            "unit_id": unit_id,
            "new_title": new_title,
            "classification": classification,
        }
    if action_type == "split_unit":
        _require_exact_fields(spec, _SPLIT_FIELDS, owner=f"action_specs[{index}]")
        return {
            "unit_id": _require_nonempty_string(
                spec.get("unit_id"),
                owner=f"action_specs[{index}].unit_id",
            ),
            "at_block_id": _require_nonempty_string(
                spec.get("at_block_id"),
                owner=f"action_specs[{index}].at_block_id",
            ),
            "left_title": _validate_title(
                spec.get("left_title"),
                owner=f"action_specs[{index}].left_title",
            ),
            "right_title": _validate_title(
                spec.get("right_title"),
                owner=f"action_specs[{index}].right_title",
            ),
            "left_classification": _validate_classification(
                spec.get("left_classification"),
                owner=f"action_specs[{index}].left_classification",
            ),
            "right_classification": _validate_classification(
                spec.get("right_classification"),
                owner=f"action_specs[{index}].right_classification",
            ),
        }
    if action_type == "merge_adjacent_units":
        _require_exact_fields(spec, _MERGE_FIELDS, owner=f"action_specs[{index}]")
        return {
            "left_unit_id": _require_nonempty_string(
                spec.get("left_unit_id"),
                owner=f"action_specs[{index}].left_unit_id",
            ),
            "right_unit_id": _require_nonempty_string(
                spec.get("right_unit_id"),
                owner=f"action_specs[{index}].right_unit_id",
            ),
            "new_title": _validate_title(
                spec.get("new_title"),
                owner=f"action_specs[{index}].new_title",
            ),
            "classification": _validate_classification(
                spec.get("classification"),
                owner=f"action_specs[{index}].classification",
            ),
        }
    raise DraftStructureError(
        f"action_specs[{index}].action_type must be one of {list(ACTION_TYPES)}"
    )


def build_correction_plan(
    report: dict[str, Any],
    action_specs: list[dict[str, Any]],
    *,
    proposer: dict[str, str],
) -> dict[str, Any]:
    _validate_report_shape(report)
    if not isinstance(proposer, dict):
        raise DraftStructureError("proposer must be an object")
    _require_exact_fields(proposer, _PROPOSER_FIELDS, owner="proposer")
    if proposer.get("kind") not in PROPOSER_KINDS:
        raise DraftStructureError(
            f"proposer.kind must be one of {list(PROPOSER_KINDS)}"
        )
    _require_nonempty_string(
        proposer.get("identifier"),
        owner="proposer.identifier",
    )
    if not isinstance(action_specs, list):
        raise DraftStructureError("action_specs must be a list")

    report_units = report["units"]
    by_id = {row["unit_id"]: row for row in report_units}
    order = {row["unit_id"]: index for index, row in enumerate(report_units)}
    prepared: list[dict[str, Any]] = []
    claim_counts: dict[str, int] = {}
    for index, spec in enumerate(action_specs):
        action_type = str(spec.get("action_type") or "") if isinstance(spec, dict) else ""
        parameters = _validate_action_spec(spec, index=index)
        targets = _action_targets(action_type, parameters)
        for target in targets:
            if target in by_id:
                claim_counts[target] = claim_counts.get(target, 0) + 1
        prepared.append(
            {
                "action_type": action_type,
                "parameters": parameters,
                "target_unit_ids": targets,
            }
        )

    sealed: list[dict[str, Any]] = []
    action_fingerprint_counts: dict[str, int] = {}
    for row in prepared:
        action_type = row["action_type"]
        parameters = row["parameters"]
        targets = row["target_unit_ids"]
        reason: str | None = None
        if any(target not in by_id for target in targets):
            reason = "unknown_unit"
        elif any(claim_counts.get(target, 0) > 1 for target in targets):
            reason = "conflicting_actions"
        elif action_type == "split_unit":
            block_ids = by_id[targets[0]]["block_ids"]
            if parameters["at_block_id"] not in block_ids[1:]:
                reason = "invalid_split_boundary"
        elif action_type == "merge_adjacent_units":
            if order[targets[1]] != order[targets[0]] + 1:
                reason = "units_not_adjacent"
        if reason is None and proposer["kind"] != "human":
            reason = "non_human_requires_review"
        status = "candidate" if reason is None else "review_required"
        action_payload = {
            "action_type": action_type,
            "parameters": parameters,
            "report_sha256": report["integrity"]["payload_sha256"],
        }
        action_fingerprint = canonical_json_sha256(action_payload)
        action_fingerprint_counts[action_fingerprint] = (
            action_fingerprint_counts.get(action_fingerprint, 0) + 1
        )
        sealed.append(
            {
                "action_id": (
                    f"act_{action_fingerprint[:18]}_"
                    f"{action_fingerprint_counts[action_fingerprint]:02d}"
                ),
                "action_type": action_type,
                "status": status,
                "reason": reason,
                "target_unit_ids": targets,
                "before_sha256": _unit_before_sha256(targets, by_id),
                "parameters": parameters,
            }
        )
    sealed.sort(
        key=lambda row: (
            min(
                (
                    order.get(unit_id, len(order))
                    for unit_id in row["target_unit_ids"]
                ),
                default=len(order),
            ),
            row["action_id"],
        )
    )
    plan: dict[str, Any] = {
        "schema_version": CORRECTION_PLAN_VERSION,
        "doc_id": report["doc_id"],
        "report_sha256": report["integrity"]["payload_sha256"],
        "inputs": copy.deepcopy(report["inputs"]),
        "proposer": copy.deepcopy(proposer),
        "actions": sealed,
    }
    plan["integrity"] = {
        "action_count": len(sealed),
        "payload_sha256": canonical_json_sha256(plan),
    }
    return plan


def build_plan_with_executor(
    executor: DraftStructureExecutor,
    report: dict[str, Any],
    *,
    proposer: dict[str, str],
) -> dict[str, Any]:
    report_copy = copy.deepcopy(report)
    action_specs = executor.propose(report_copy)
    if report_copy != report:
        raise DraftStructureError("executor mutated its report input")
    return build_correction_plan(
        report,
        action_specs,
        proposer=proposer,
    )


def _validate_plan(
    plan: dict[str, Any],
    report: dict[str, Any],
) -> None:
    if not isinstance(plan, dict):
        raise DraftStructureError("correction plan must be an object")
    _require_exact_fields(plan, _PLAN_FIELDS, owner="plan")
    if plan.get("schema_version") != CORRECTION_PLAN_VERSION:
        raise DraftStructureError(
            f"plan.schema_version must be {CORRECTION_PLAN_VERSION}"
        )
    if plan.get("doc_id") != report.get("doc_id"):
        raise DraftStructureError("plan doc_id differs from report")
    if plan.get("report_sha256") != report["integrity"]["payload_sha256"]:
        raise DraftStructureError("plan report identity is stale")
    if plan.get("inputs") != report.get("inputs"):
        raise DraftStructureError("plan input identities differ from report")
    proposer = plan.get("proposer")
    if not isinstance(proposer, dict):
        raise DraftStructureError("plan.proposer must be an object")
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise DraftStructureError("plan.actions must be a list")
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise DraftStructureError(f"plan.actions[{index}] must be an object")
        _require_exact_fields(
            action,
            _PLAN_ACTION_FIELDS,
            owner=f"plan.actions[{index}]",
        )
    integrity = plan.get("integrity")
    if not isinstance(integrity, dict):
        raise DraftStructureError("plan.integrity must be an object")
    _require_exact_fields(
        integrity,
        _PLAN_INTEGRITY_FIELDS,
        owner="plan.integrity",
    )
    if integrity.get("action_count") != len(actions):
        raise DraftStructureError("plan action_count differs")
    if integrity.get("payload_sha256") != canonical_json_sha256(
        _payload_without_integrity(plan)
    ):
        raise DraftStructureError("plan payload hash differs")

    raw_specs = [
        {
            "action_type": action["action_type"],
            **copy.deepcopy(action["parameters"]),
        }
        for action in actions
    ]
    rebuilt = build_correction_plan(
        report,
        raw_specs,
        proposer=proposer,
    )
    if rebuilt != plan:
        raise DraftStructureError("plan decisions or guards differ from code-sealed plan")


def validate_authoritative_draft_structure_report(
    report: dict[str, Any],
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    *,
    package_root: str | Path | None = None,
) -> None:
    """Rebuild a report from the complete package before an advisory boundary."""

    _validate_report_shape(report)
    skeleton = report["global_skeleton"]
    policy = GlobalStructurePolicy.from_payload(skeleton["policy"])
    expected = build_draft_structure_report(
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        project_state,
        package_root=package_root,
        global_policy=policy,
    )
    if expected != report:
        raise DraftStructureError("draft structure report is stale or tampered")


def hierarchy_input_identities(report: dict[str, Any]) -> dict[str, Any]:
    """Return the closed identity set shared by A2 plans, prompts, and overlays."""

    _validate_report_shape(report)
    skeleton = report["global_skeleton"]
    policy = skeleton["policy"]
    identities = copy.deepcopy(report["inputs"])
    identities.update(
        {
            "report": _identity(report["schema_version"], report),
            "skeleton": _identity(skeleton["schema_version"], skeleton),
            "policy": _identity(policy["schema_version"], policy),
        }
    )
    _require_exact_fields(
        identities,
        _HIERARCHY_INPUT_FIELDS,
        owner="hierarchy inputs",
    )
    return identities


def _hierarchy_current_assignments(
    report: dict[str, Any],
) -> tuple[list[str], dict[str, str | None]]:
    _validate_report_shape(report)
    report_unit_ids = [str(row["unit_id"]) for row in report["units"]]
    outline = report["global_skeleton"]["outline"]
    outline_unit_ids = [str(row["unit_id"]) for row in outline]
    if report_unit_ids != outline_unit_ids:
        raise DraftStructureError("hierarchy report and outline unit order differ")
    assignments: dict[str, str | None] = {}
    for index, row in enumerate(outline):
        parent = row.get("parent_unit_id")
        if parent is not None and not isinstance(parent, str):
            raise DraftStructureError(
                f"hierarchy outline[{index}].parent_unit_id must be string or null"
            )
        assignments[outline_unit_ids[index]] = parent
    return report_unit_ids, assignments


def _hierarchy_graph_issue(
    unit_ids: list[str],
    assignments: dict[str, str | None],
) -> str | None:
    if len(unit_ids) != len(set(unit_ids)):
        return "duplicate_unit_id"
    if set(assignments) != set(unit_ids):
        return "assignment_cover_mismatch"
    unit_set = set(unit_ids)
    order = {unit_id: index for index, unit_id in enumerate(unit_ids)}
    for child_id in unit_ids:
        parent_id = assignments[child_id]
        if parent_id is not None and parent_id not in unit_set:
            return "unknown_parent_unit"
        if parent_id == child_id:
            return "self_parent"

    for start_id in unit_ids:
        seen: set[str] = set()
        cursor: str | None = start_id
        while cursor is not None:
            if cursor in seen:
                return "cycle"
            seen.add(cursor)
            cursor = assignments[cursor]

    for child_id in unit_ids:
        parent_id = assignments[child_id]
        if parent_id is not None and order[parent_id] >= order[child_id]:
            return "child_before_parent"

    children: dict[str, list[str]] = {unit_id: [] for unit_id in unit_ids}
    for child_id in unit_ids:
        parent_id = assignments[child_id]
        if parent_id is not None:
            children[parent_id].append(child_id)
    for root_id in unit_ids:
        subtree: set[str] = set()
        stack = [root_id]
        while stack:
            unit_id = stack.pop()
            if unit_id in subtree:
                continue
            subtree.add(unit_id)
            stack.extend(reversed(children[unit_id]))
        positions = sorted(order[unit_id] for unit_id in subtree)
        if positions != list(range(positions[0], positions[-1] + 1)):
            return "non_contiguous_or_crossing_subtree"
    return None


def _validate_hierarchy_action_spec(
    spec: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise DraftStructureError(
            f"hierarchy action_specs[{index}] must be an object"
        )
    action_type = spec.get("action_type")
    if action_type == "set_parent":
        _require_exact_fields(
            spec,
            _HIERARCHY_SET_PARENT_FIELDS,
            owner=f"hierarchy action_specs[{index}]",
        )
        return {
            "action_type": "set_parent",
            "child_unit_id": _require_nonempty_string(
                spec.get("child_unit_id"),
                owner=f"hierarchy action_specs[{index}].child_unit_id",
            ),
            "parent_unit_id": _require_nonempty_string(
                spec.get("parent_unit_id"),
                owner=f"hierarchy action_specs[{index}].parent_unit_id",
            ),
        }
    if action_type == "clear_parent":
        _require_exact_fields(
            spec,
            _HIERARCHY_CLEAR_PARENT_FIELDS,
            owner=f"hierarchy action_specs[{index}]",
        )
        return {
            "action_type": "clear_parent",
            "child_unit_id": _require_nonempty_string(
                spec.get("child_unit_id"),
                owner=f"hierarchy action_specs[{index}].child_unit_id",
            ),
            "parent_unit_id": None,
        }
    raise DraftStructureError(
        "hierarchy action type must be set_parent or clear_parent"
    )


def build_hierarchy_plan(
    report: dict[str, Any],
    action_specs: list[dict[str, Any]],
    *,
    proposer: dict[str, str],
) -> dict[str, Any]:
    """Code-seal a separate A2 plan without changing canonical structure."""

    _validate_report_shape(report)
    if not isinstance(proposer, dict):
        raise DraftStructureError("hierarchy proposer must be an object")
    _require_exact_fields(proposer, _PROPOSER_FIELDS, owner="hierarchy proposer")
    if proposer.get("kind") not in PROPOSER_KINDS:
        raise DraftStructureError(
            f"hierarchy proposer.kind must be one of {list(PROPOSER_KINDS)}"
        )
    _require_nonempty_string(
        proposer.get("identifier"), owner="hierarchy proposer.identifier"
    )
    if not isinstance(action_specs, list):
        raise DraftStructureError("hierarchy action_specs must be a list")

    unit_ids, current = _hierarchy_current_assignments(report)
    unit_set = set(unit_ids)
    order = {unit_id: index for index, unit_id in enumerate(unit_ids)}
    prepared = [
        _validate_hierarchy_action_spec(spec, index=index)
        for index, spec in enumerate(action_specs)
    ]
    claim_counts: dict[str, int] = {}
    for row in prepared:
        child_id = row["child_unit_id"]
        claim_counts[child_id] = claim_counts.get(child_id, 0) + 1

    proposed = copy.deepcopy(current)
    reasons: list[str | None] = []
    for row in prepared:
        child_id = row["child_unit_id"]
        parent_id = row["parent_unit_id"]
        reason: str | None = None
        if child_id not in unit_set:
            reason = "unknown_child_unit"
        elif parent_id is not None and parent_id not in unit_set:
            reason = "unknown_parent_unit"
        elif claim_counts[child_id] > 1:
            reason = "conflicting_actions"
        elif parent_id == current[child_id]:
            reason = "no_change"
        if reason is None:
            proposed[child_id] = parent_id
        reasons.append(reason)

    graph_issue = _hierarchy_graph_issue(unit_ids, proposed)
    if graph_issue is not None:
        reasons = [reason or graph_issue for reason in reasons]
    elif proposer["kind"] != "human":
        reasons = [reason or "non_human_requires_review" for reason in reasons]

    sealed_actions: list[dict[str, Any]] = []
    fingerprint_counts: dict[str, int] = {}
    for row, reason in zip(prepared, reasons, strict=True):
        child_id = row["child_unit_id"]
        before_parent = current.get(child_id)
        before_payload = {
            "child_unit_id": child_id,
            "parent_unit_id": before_parent,
        }
        fingerprint_payload = {
            **row,
            "report_sha256": report["integrity"]["payload_sha256"],
        }
        fingerprint = canonical_json_sha256(fingerprint_payload)
        fingerprint_counts[fingerprint] = fingerprint_counts.get(fingerprint, 0) + 1
        sealed_actions.append(
            {
                "action_id": (
                    f"hact_{fingerprint[:18]}_"
                    f"{fingerprint_counts[fingerprint]:02d}"
                ),
                "action_type": row["action_type"],
                "status": "candidate" if reason is None else "review_required",
                "reason": reason,
                "child_unit_id": child_id,
                "parent_unit_id": row["parent_unit_id"],
                "before_parent_unit_id": before_parent,
                "before_sha256": (
                    canonical_json_sha256(before_payload)
                    if child_id in current
                    else None
                ),
            }
        )
    sealed_actions.sort(
        key=lambda row: (
            order.get(row["child_unit_id"], len(order)),
            row["action_id"],
        )
    )
    plan: dict[str, Any] = {
        "schema_version": HIERARCHY_PLAN_VERSION,
        "doc_id": report["doc_id"],
        "state": HIERARCHY_STATE,
        "inputs": hierarchy_input_identities(report),
        "proposer": copy.deepcopy(proposer),
        "actions": sealed_actions,
    }
    plan["integrity"] = {
        "action_count": len(sealed_actions),
        "payload_sha256": canonical_json_sha256(plan),
    }
    return plan


def _validate_hierarchy_plan(
    plan: dict[str, Any],
    report: dict[str, Any],
) -> None:
    if not isinstance(plan, dict):
        raise DraftStructureError("hierarchy plan must be an object")
    _require_exact_fields(plan, _HIERARCHY_PLAN_FIELDS, owner="hierarchy plan")
    if plan.get("schema_version") != HIERARCHY_PLAN_VERSION:
        raise DraftStructureError("hierarchy plan version differs")
    if plan.get("doc_id") != report.get("doc_id"):
        raise DraftStructureError("hierarchy plan doc_id differs")
    if plan.get("state") != HIERARCHY_STATE:
        raise DraftStructureError("hierarchy plan state differs")
    if plan.get("inputs") != hierarchy_input_identities(report):
        raise DraftStructureError("hierarchy plan input identities differ")
    proposer = plan.get("proposer")
    if not isinstance(proposer, dict):
        raise DraftStructureError("hierarchy plan proposer must be an object")
    _require_exact_fields(proposer, _PROPOSER_FIELDS, owner="hierarchy proposer")
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise DraftStructureError("hierarchy plan actions must be a list")
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise DraftStructureError(
                f"hierarchy plan.actions[{index}] must be an object"
            )
        _require_exact_fields(
            action,
            _HIERARCHY_PLAN_ACTION_FIELDS,
            owner=f"hierarchy plan.actions[{index}]",
        )
    integrity = plan.get("integrity")
    if not isinstance(integrity, dict):
        raise DraftStructureError("hierarchy plan integrity must be an object")
    _require_exact_fields(
        integrity,
        _HIERARCHY_PLAN_INTEGRITY_FIELDS,
        owner="hierarchy plan integrity",
    )
    if integrity.get("action_count") != len(actions):
        raise DraftStructureError("hierarchy plan action_count differs")
    if integrity.get("payload_sha256") != canonical_json_sha256(
        _payload_without_integrity(plan)
    ):
        raise DraftStructureError("hierarchy plan payload hash differs")
    raw_specs = [
        {
            "action_type": action["action_type"],
            "child_unit_id": action["child_unit_id"],
            **(
                {"parent_unit_id": action["parent_unit_id"]}
                if action["action_type"] == "set_parent"
                else {}
            ),
        }
        for action in actions
    ]
    rebuilt = build_hierarchy_plan(report, raw_specs, proposer=proposer)
    if rebuilt != plan:
        raise DraftStructureError(
            "hierarchy plan decisions or guards differ from code-sealed plan"
        )


def _effective_hierarchy_assignments(
    report: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[list[str], dict[str, str | None]]:
    unit_ids, assignments = _hierarchy_current_assignments(report)
    for action in plan["actions"]:
        if action["status"] != "candidate":
            raise DraftStructureError(
                "hierarchy plan contains actions that still require review"
            )
        assignments[action["child_unit_id"]] = action["parent_unit_id"]
    issue = _hierarchy_graph_issue(unit_ids, assignments)
    if issue is not None:
        raise DraftStructureError(f"hierarchy graph is invalid: {issue}")
    return unit_ids, assignments


def _build_hierarchy_overlay_unchecked(
    report: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    unit_ids, assignments = _effective_hierarchy_assignments(report, plan)
    overlay: dict[str, Any] = {
        "schema_version": HIERARCHY_OVERLAY_VERSION,
        "doc_id": report["doc_id"],
        "state": HIERARCHY_STATE,
        "inputs": hierarchy_input_identities(report),
        "plan_sha256": plan["integrity"]["payload_sha256"],
        "rows": [
            {
                "child_unit_id": unit_id,
                "order_index": index,
                "parent_unit_id": assignments[unit_id],
            }
            for index, unit_id in enumerate(unit_ids)
        ],
    }
    overlay["integrity"] = {
        "row_count": len(unit_ids),
        "payload_sha256": canonical_json_sha256(overlay),
    }
    return overlay


def validate_hierarchy_overlay(
    overlay: dict[str, Any],
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    report: dict[str, Any],
    plan: dict[str, Any],
    *,
    package_root: str | Path | None = None,
) -> None:
    validate_authoritative_draft_structure_report(
        report,
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        project_state,
        package_root=package_root,
    )
    _validate_hierarchy_plan(plan, report)
    if plan["proposer"]["kind"] != "human":
        raise DraftStructureError("hierarchy overlay requires explicit human approval")
    if not isinstance(overlay, dict):
        raise DraftStructureError("hierarchy overlay must be an object")
    _require_exact_fields(
        overlay,
        _HIERARCHY_OVERLAY_FIELDS,
        owner="hierarchy overlay",
    )
    if overlay.get("schema_version") != HIERARCHY_OVERLAY_VERSION:
        raise DraftStructureError("hierarchy overlay version differs")
    if overlay.get("doc_id") != report.get("doc_id"):
        raise DraftStructureError("hierarchy overlay doc_id differs")
    if overlay.get("state") != HIERARCHY_STATE:
        raise DraftStructureError("hierarchy overlay state differs")
    if overlay.get("inputs") != hierarchy_input_identities(report):
        raise DraftStructureError("hierarchy overlay input identities differ")
    if overlay.get("plan_sha256") != plan["integrity"]["payload_sha256"]:
        raise DraftStructureError("hierarchy overlay plan identity differs")
    rows = overlay.get("rows")
    if not isinstance(rows, list):
        raise DraftStructureError("hierarchy overlay rows must be a list")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DraftStructureError(
                f"hierarchy overlay.rows[{index}] must be an object"
            )
        _require_exact_fields(
            row,
            _HIERARCHY_OVERLAY_ROW_FIELDS,
            owner=f"hierarchy overlay.rows[{index}]",
        )
    integrity = overlay.get("integrity")
    if not isinstance(integrity, dict):
        raise DraftStructureError("hierarchy overlay integrity must be an object")
    _require_exact_fields(
        integrity,
        _HIERARCHY_OVERLAY_INTEGRITY_FIELDS,
        owner="hierarchy overlay integrity",
    )
    if integrity.get("row_count") != len(rows):
        raise DraftStructureError("hierarchy overlay row_count differs")
    if integrity.get("payload_sha256") != canonical_json_sha256(
        _payload_without_integrity(overlay)
    ):
        raise DraftStructureError("hierarchy overlay payload hash differs")
    expected = _build_hierarchy_overlay_unchecked(report, plan)
    if expected != overlay:
        raise DraftStructureError(
            "hierarchy overlay differs from authoritative approved plan"
        )


def apply_hierarchy_plan(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    report: dict[str, Any],
    plan: dict[str, Any],
    *,
    package_root: str | Path | None = None,
) -> dict[str, Any]:
    """Emit a sealed A2 overlay while preserving the canonical package."""

    before = {
        "document": canonical_json_sha256(document),
        "structure": canonical_json_sha256(structure_manifest),
        "asset_manifest": canonical_json_sha256(asset_manifest),
        "admitted_projection": canonical_json_sha256(admitted_projection),
        "project_state": canonical_json_sha256(project_state),
    }
    validate_authoritative_draft_structure_report(
        report,
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        project_state,
        package_root=package_root,
    )
    _validate_hierarchy_plan(plan, report)
    if plan["proposer"]["kind"] != "human":
        raise DraftStructureError("hierarchy apply requires explicit human approval")
    overlay = _build_hierarchy_overlay_unchecked(report, plan)
    validate_hierarchy_overlay(
        overlay,
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        project_state,
        report,
        plan,
        package_root=package_root,
    )
    after = {
        "document": canonical_json_sha256(document),
        "structure": canonical_json_sha256(structure_manifest),
        "asset_manifest": canonical_json_sha256(asset_manifest),
        "admitted_projection": canonical_json_sha256(admitted_projection),
        "project_state": canonical_json_sha256(project_state),
    }
    if after != before:
        raise DraftStructureError("hierarchy apply mutated the canonical package")
    return overlay


def _policy_for_block(source_kind: str, classification: str) -> str:
    if classification == "review":
        return "review"
    if classification in {"preserve", "exclude"}:
        return classification
    if source_kind in _RICH_TRANSLATE_STRUCTURED:
        return "translate_structured"
    if source_kind in _PRESERVE_SOURCE_KINDS:
        return "preserve"
    return "translate"


def _role_for_classification(base_role: str, classification: str) -> str:
    if classification == "translate":
        return "content_unit"
    if classification == "preserve":
        return base_role if base_role in {"front_matter", "container"} else "container"
    if classification == "exclude":
        return "back_matter"
    return base_role or "unknown"


def _corrected_identifier(
    doc_id: str,
    action_id: str,
    suffix: str,
    *,
    owner: str,
) -> str:
    doc_slug = re.sub(r"[^A-Za-z0-9_]+", "_", doc_id).strip("_") or "doc"
    digest = action_id.removeprefix("act_")[:16]
    prefix = "u" if owner == "unit" else doc_slug
    return f"{prefix}_corr_{digest}_{suffix}"


def _update_record(
    base: dict[str, Any],
    *,
    unit_id: str,
    chapter_id: str,
    title: str,
    classification: str,
    action_id: str,
    proposer_kind: str,
    new_identity: bool,
) -> dict[str, Any]:
    record = copy.deepcopy(base)
    record.update(
        {
            "unit_id": unit_id,
            "chapter_id": chapter_id,
            "title": title,
            "role": _role_for_classification(
                str(base.get("role") or "unknown"),
                classification,
            ),
            "translation_policy": classification,
            "confidence": 1.0 if proposer_kind == "human" else 0.0,
            "review_required": (
                classification == "review" or proposer_kind != "human"
            ),
        }
    )
    evidence = [str(item) for item in base.get("evidence") or []]
    evidence.append(f"draft_structure_action:{action_id}")
    record["evidence"] = list(dict.fromkeys(evidence))
    if new_identity:
        if "parent_unit_id" in record:
            record["parent_unit_id"] = None
        if "source_target" in record:
            record["source_target"] = None
    return record


def _working_units(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
) -> list[_WorkingUnit]:
    chapters, _blocks = _flatten_document(document)
    units = structure_manifest.get("units")
    if not isinstance(units, list) or len(units) != len(chapters):
        raise DraftStructureError(
            "structure units must map one-to-one to document chapters"
        )
    result: list[_WorkingUnit] = []
    for unit, chapter in zip(units, chapters, strict=True):
        unit_id = str(unit.get("unit_id") or "")
        result.append(
            _WorkingUnit(
                record=copy.deepcopy(unit),
                chapter_template=copy.deepcopy(chapter),
                blocks=copy.deepcopy(chapter.get("blocks") or []),
                source_unit_ids=(unit_id,),
                classification=_classification_for_unit(unit),
            )
        )
    return result


def _apply_candidate_actions(
    original: list[_WorkingUnit],
    plan: dict[str, Any],
    *,
    doc_id: str,
) -> list[_WorkingUnit]:
    proposer_kind = plan["proposer"]["kind"]
    candidates = {
        unit_id: action
        for action in plan["actions"]
        if action["status"] == "candidate"
        for unit_id in action["target_unit_ids"]
    }
    review_targets = {
        unit_id
        for action in plan["actions"]
        if action["status"] == "review_required"
        for unit_id in action["target_unit_ids"]
    }
    result: list[_WorkingUnit] = []
    index = 0
    while index < len(original):
        current = original[index]
        unit_id = current.source_unit_ids[0]
        action = candidates.get(unit_id)
        if action is None:
            copied = copy.deepcopy(current)
            if unit_id in review_targets:
                copied.record["review_required"] = True
                copied.record["translation_policy"] = "review"
                copied.classification = "review"
            result.append(copied)
            index += 1
            continue

        parameters = action["parameters"]
        action_id = action["action_id"]
        if action["action_type"] == "update_unit":
            classification = parameters["classification"] or current.classification
            title = parameters["new_title"] or str(current.record.get("title") or "")
            record = _update_record(
                current.record,
                unit_id=unit_id,
                chapter_id=str(current.record["chapter_id"]),
                title=title,
                classification=classification,
                action_id=action_id,
                proposer_kind=proposer_kind,
                new_identity=False,
            )
            result.append(
                _WorkingUnit(
                    record=record,
                    chapter_template=copy.deepcopy(current.chapter_template),
                    blocks=copy.deepcopy(current.blocks),
                    source_unit_ids=current.source_unit_ids,
                    classification=classification,
                )
            )
            index += 1
            continue

        if action["action_type"] == "split_unit":
            block_ids = [str(block["block_id"]) for block in current.blocks]
            split_index = block_ids.index(parameters["at_block_id"])
            for suffix, blocks, title, classification in (
                (
                    "a",
                    current.blocks[:split_index],
                    parameters["left_title"],
                    parameters["left_classification"],
                ),
                (
                    "b",
                    current.blocks[split_index:],
                    parameters["right_title"],
                    parameters["right_classification"],
                ),
            ):
                new_unit_id = _corrected_identifier(
                    doc_id,
                    action_id,
                    suffix,
                    owner="unit",
                )
                new_chapter_id = _corrected_identifier(
                    doc_id,
                    action_id,
                    suffix,
                    owner="chapter",
                )
                record = _update_record(
                    current.record,
                    unit_id=new_unit_id,
                    chapter_id=new_chapter_id,
                    title=title,
                    classification=classification,
                    action_id=action_id,
                    proposer_kind=proposer_kind,
                    new_identity=True,
                )
                result.append(
                    _WorkingUnit(
                        record=record,
                        chapter_template=copy.deepcopy(current.chapter_template),
                        blocks=copy.deepcopy(blocks),
                        source_unit_ids=current.source_unit_ids,
                        classification=classification,
                    )
                )
            index += 1
            continue

        right = original[index + 1]
        classification = parameters["classification"]
        new_unit_id = _corrected_identifier(
            doc_id,
            action_id,
            "m",
            owner="unit",
        )
        new_chapter_id = _corrected_identifier(
            doc_id,
            action_id,
            "m",
            owner="chapter",
        )
        record = _update_record(
            current.record,
            unit_id=new_unit_id,
            chapter_id=new_chapter_id,
            title=parameters["new_title"],
            classification=classification,
            action_id=action_id,
            proposer_kind=proposer_kind,
            new_identity=True,
        )
        result.append(
            _WorkingUnit(
                record=record,
                chapter_template=copy.deepcopy(current.chapter_template),
                blocks=copy.deepcopy(current.blocks + right.blocks),
                source_unit_ids=current.source_unit_ids + right.source_unit_ids,
                classification=classification,
            )
        )
        index += 2
    return result


def _rebuild_document(
    document: dict[str, Any],
    units: list[_WorkingUnit],
) -> dict[str, Any]:
    corrected = copy.deepcopy(document)
    chapters: list[dict[str, Any]] = []
    for chapter_order, unit in enumerate(units, start=1):
        blocks: list[dict[str, Any]] = []
        for block_order, block in enumerate(unit.blocks, start=1):
            row = copy.deepcopy(block)
            row["order_index"] = block_order
            row["is_chapter_opening"] = block_order == 1
            blocks.append(row)
        chapter = copy.deepcopy(unit.chapter_template)
        chapter.update(
            {
                "chapter_id": unit.record["chapter_id"],
                "order_index": chapter_order,
                "title": unit.record["title"],
                "blocks": blocks,
            }
        )
        chapters.append(chapter)
    corrected["chapters"] = chapters
    return corrected


def _rebuild_structure(
    structure_manifest: dict[str, Any],
    units: list[_WorkingUnit],
) -> dict[str, Any]:
    corrected = copy.deepcopy(structure_manifest)
    source_map = corrected.get("source_map")
    if not isinstance(source_map, list):
        raise DraftStructureError("structure.source_map must be a list")
    source_kind_by_block = {
        str(row.get("block_id") or ""): str(
            row.get("source_block_kind") or "paragraph"
        )
        for row in source_map
        if isinstance(row, dict)
    }

    offset = 0
    structure_units: list[dict[str, Any]] = []
    block_policies: list[dict[str, str]] = []
    review_unit_ids: list[str] = []
    review_chapter_ids: list[str] = []
    translatable_chapter_ids: list[str] = []
    for order_index, unit in enumerate(units):
        count = len(unit.blocks)
        record = copy.deepcopy(unit.record)
        record["order_index"] = order_index
        record["block_range"] = [offset, offset + count]
        structure_units.append(record)
        if record["review_required"]:
            review_unit_ids.append(record["unit_id"])
            review_chapter_ids.append(record["chapter_id"])
        elif (
            record["role"] == "content_unit"
            and record["translation_policy"] == "translate"
        ):
            translatable_chapter_ids.append(record["chapter_id"])
        for block in unit.blocks:
            block_id = str(block["block_id"])
            block_policies.append(
                {
                    "block_id": block_id,
                    "translation_policy": _policy_for_block(
                        source_kind_by_block.get(block_id, "paragraph"),
                        unit.classification,
                    ),
                }
            )
        offset += count

    corrected["units"] = structure_units
    corrected["block_policies"] = block_policies
    corrected["review_required_unit_ids"] = review_unit_ids
    corrected["review_required_chapter_ids"] = review_chapter_ids
    corrected["translatable_chapter_ids"] = translatable_chapter_ids
    corrected["normalizer_version"] = (
        f"{structure_manifest.get('normalizer_version')}"
        f"+{CORRECTION_ENGINE_VERSION}"
    )
    corrected.pop("structure_sha256", None)
    corrected["structure_sha256"] = canonical_json_sha256(corrected)
    return corrected


def apply_correction_plan(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    report: dict[str, Any],
    plan: dict[str, Any],
    *,
    package_root: str | Path | None = None,
) -> DraftStructureResult:
    skeleton = report.get("global_skeleton") if isinstance(report, dict) else None
    global_policy = (
        GlobalStructurePolicy.from_payload(skeleton.get("policy"))
        if isinstance(skeleton, dict)
        else None
    )
    expected_report = build_draft_structure_report(
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        project_state,
        package_root=package_root,
        global_policy=global_policy,
    )
    if expected_report != report:
        raise DraftStructureError("draft structure report is stale or tampered")
    _validate_plan(plan, report)
    doc_id = str(document["doc_id"])
    before_block_ids = [
        str(block["block_id"])
        for _chapter in document["chapters"]
        for block in _chapter["blocks"]
    ]
    before_text = [
        (str(block["block_id"]), block["source_text"], block["clean_text"])
        for _chapter in document["chapters"]
        for block in _chapter["blocks"]
    ]
    original_units = _working_units(document, structure_manifest)
    units = _apply_candidate_actions(
        original_units,
        plan,
        doc_id=doc_id,
    )
    if units == original_units:
        corrected_document = copy.deepcopy(document)
        corrected_structure = copy.deepcopy(structure_manifest)
    else:
        corrected_document = _rebuild_document(document, units)
        corrected_structure = _rebuild_structure(structure_manifest, units)
    after_block_ids = [
        str(block["block_id"])
        for chapter in corrected_document["chapters"]
        for block in chapter["blocks"]
    ]
    after_text = [
        (str(block["block_id"]), block["source_text"], block["clean_text"])
        for chapter in corrected_document["chapters"]
        for block in chapter["blocks"]
    ]
    if after_block_ids != before_block_ids:
        raise DraftStructureError(
            "correction plan changed block order, coverage, or identity"
        )
    if after_text != before_text:
        raise DraftStructureError("correction plan changed source or clean text")
    source_format = str(corrected_structure["source"]["format"])
    normalization_receipt = validate_normalization_contract(
        corrected_document,
        corrected_structure,
        expected_format=source_format,
    )
    accepted = sum(
        action["status"] == "candidate"
        for action in plan["actions"]
    )
    review_required = len(plan["actions"]) - accepted
    correction_receipt: dict[str, Any] = {
        "schema_version": CORRECTION_RECEIPT_VERSION,
        "doc_id": doc_id,
        "state": "experimental_non_load_bearing",
        "project_state_sha256": report["inputs"]["project_state"]["sha256"],
        "report_sha256": report["integrity"]["payload_sha256"],
        "plan_sha256": plan["integrity"]["payload_sha256"],
        "before": {
            "document_sha256": canonical_json_sha256(document),
            "structure_sha256": canonical_json_sha256(structure_manifest),
            "asset_manifest_sha256": canonical_json_sha256(asset_manifest),
            "admitted_projection_sha256": canonical_json_sha256(
                admitted_projection
            ),
            "source_sha256": str(structure_manifest["source"]["sha256"]),
        },
        "after": {
            "document_sha256": canonical_json_sha256(corrected_document),
            "structure_sha256": canonical_json_sha256(corrected_structure),
            "source_sha256": str(corrected_structure["source"]["sha256"]),
        },
        "actions": {
            "accepted": accepted,
            "review_required": review_required,
        },
    }
    correction_receipt["receipt_sha256"] = canonical_json_sha256(
        correction_receipt
    )
    return DraftStructureResult(
        document=corrected_document,
        structure_manifest=corrected_structure,
        normalization_receipt=normalization_receipt,
        correction_receipt=correction_receipt,
    )


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_experimental_draft_package(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    report: dict[str, Any],
    plan: dict[str, Any],
    output_dir: str | Path,
    *,
    package_root: str | Path | None = None,
) -> DraftStructureWriteResult:
    destination = Path(output_dir).resolve()
    if destination.exists():
        raise DraftStructureError(
            "experimental output directory must not already exist"
        )
    source = Path(str(structure_manifest["source"]["path"])).resolve()
    if not source.is_file():
        raise DraftStructureError(f"source file is missing: {source}")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_sha256 != structure_manifest["source"]["sha256"]:
        raise DraftStructureError("source hash differs before correction")

    result = apply_correction_plan(
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        project_state,
        report,
        plan,
        package_root=package_root,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
    )
    try:
        _atomic_json_write(temporary / "document.json", result.document)
        _atomic_json_write(
            temporary / "structure_manifest.json",
            result.structure_manifest,
        )
        _atomic_json_write(
            temporary / "normalization_receipt.json",
            result.normalization_receipt,
        )
        write_result = materialize_source_package(
            result.document,
            result.structure_manifest,
            temporary,
        )
        output_asset_manifest = json.loads(
            write_result.asset_manifest_path.read_text(encoding="utf-8")
        )
        output_projection = json.loads(
            write_result.admitted_projection_path.read_text(encoding="utf-8")
        )
        correction_receipt = copy.deepcopy(result.correction_receipt)
        correction_receipt["after"].update(
            {
                "asset_manifest_sha256": canonical_json_sha256(
                    output_asset_manifest
                ),
                "admitted_projection_sha256": canonical_json_sha256(
                    output_projection
                ),
            }
        )
        correction_receipt["receipt_sha256"] = canonical_json_sha256(
            {
                key: value
                for key, value in correction_receipt.items()
                if key != "receipt_sha256"
            }
        )
        _atomic_json_write(
            temporary / "draft_structure_correction_receipt_v1.json",
            correction_receipt,
        )
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha256:
            raise DraftStructureError("source bytes changed during correction")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return DraftStructureWriteResult(
        output_dir=destination,
        document_path=destination / "document.json",
        structure_manifest_path=destination / "structure_manifest.json",
        admitted_projection_path=destination / "admitted_projection_v1.json",
        correction_receipt_path=(
            destination / "draft_structure_correction_receipt_v1.json"
        ),
    )


__all__ = [
    "ACTION_TYPES",
    "CLASSIFICATIONS",
    "CORRECTION_ENGINE_VERSION",
    "CORRECTION_PLAN_VERSION",
    "CORRECTION_RECEIPT_VERSION",
    "DRAFT_PROJECT_STATE_VERSION",
    "DRAFT_STRUCTURE_REPORT_VERSION",
    "GLOBAL_STRUCTURE_POLICY_VERSION",
    "GLOBAL_STRUCTURE_SKELETON_VERSION",
    "HIERARCHY_ACTION_TYPES",
    "HIERARCHY_OVERLAY_VERSION",
    "HIERARCHY_PLAN_VERSION",
    "HIERARCHY_STATE",
    "DraftStructureError",
    "DraftStructureExecutor",
    "DraftStructureFrozenError",
    "DraftStructureResult",
    "DraftStructureWriteResult",
    "GlobalStructurePolicy",
    "apply_correction_plan",
    "apply_hierarchy_plan",
    "build_correction_plan",
    "build_draft_structure_report",
    "build_global_structure_skeleton",
    "build_hierarchy_plan",
    "build_plan_with_executor",
    "hierarchy_input_identities",
    "validate_authoritative_draft_structure_report",
    "validate_draft_structure_report_shape",
    "validate_global_structure_skeleton",
    "validate_hierarchy_overlay",
    "write_experimental_draft_package",
]
