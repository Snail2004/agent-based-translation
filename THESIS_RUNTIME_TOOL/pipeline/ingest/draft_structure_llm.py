from __future__ import annotations

import copy
import inspect
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pipeline.ingest.canonical_source_package import canonical_json_sha256
from pipeline.ingest.draft_structure import (
    DraftStructureError,
    build_correction_plan,
    build_hierarchy_plan,
    hierarchy_input_identities,
    validate_authoritative_draft_structure_report,
    validate_draft_structure_report_shape,
    validate_global_structure_skeleton,
)


CONTEXT_PACK_VERSION = "draft_structure_llm_context_v1"
CONTEXT_POLICY_VERSION = "draft_structure_llm_context_policy_v1"
RESPONSE_VERSION = "draft_structure_llm_response_v1"
BOUNDARY_REPAIR_SCHEMA_DIALECT_V1 = "draft_structure_response_contract_v1"
BOUNDARY_REPAIR_SCHEMA_DIALECT_V2 = "draft_structure_response_contract_v2"
_BOUNDARY_REPAIR_SCHEMA_DIALECTS = frozenset(
    {
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    }
)
EXPANSION_VERSION = "draft_structure_llm_expansion_v1"
GLOBAL_CONTEXT_PACK_VERSION = "draft_structure_global_context_v1"
GLOBAL_RESPONSE_VERSION = "draft_structure_global_response_v1"
HIERARCHY_CONTEXT_PACK_VERSION = "draft_structure_hierarchy_context_v1"
HIERARCHY_RESPONSE_VERSION = "draft_structure_hierarchy_response_v1"

ABSTENTION_REASONS = frozenset(
    {
        "conflicting_evidence",
        "insufficient_context",
        "no_change",
        "not_applicable",
    }
)

_ACTION_FIELDS = {
    "update_unit": {"action_type", "unit_id", "new_title", "classification"},
    "split_unit": {
        "action_type",
        "unit_id",
        "at_block_id",
        "left_title",
        "right_title",
        "left_classification",
        "right_classification",
    },
    "merge_adjacent_units": {
        "action_type",
        "left_unit_id",
        "right_unit_id",
        "new_title",
        "classification",
    },
}


class StructureAssistantExecutor(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        context_pack: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class StructureContextBudget:
    max_prompt_chars: int = 48_000
    max_focus_units_per_pack: int = 8
    left_blocks: int = 2
    right_blocks: int = 3
    block_preview_chars: int = 520
    max_expansion_blocks_per_side: int = 4
    max_expansion_requests_per_document: int = 5
    expansion_preview_chars: int = 1_500
    max_global_candidates_per_pack: int = 24
    global_neighbor_units: int = 1

    def validate(self) -> None:
        integer_fields = {
            "max_prompt_chars": self.max_prompt_chars,
            "max_focus_units_per_pack": self.max_focus_units_per_pack,
            "left_blocks": self.left_blocks,
            "right_blocks": self.right_blocks,
            "block_preview_chars": self.block_preview_chars,
            "max_expansion_blocks_per_side": self.max_expansion_blocks_per_side,
            "max_expansion_requests_per_document": (
                self.max_expansion_requests_per_document
            ),
            "expansion_preview_chars": self.expansion_preview_chars,
            "max_global_candidates_per_pack": (
                self.max_global_candidates_per_pack
            ),
            "global_neighbor_units": self.global_neighbor_units,
        }
        for name, value in integer_fields.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise DraftStructureError(f"context budget {name} must be positive")
        if self.max_prompt_chars < 4_000:
            raise DraftStructureError("max_prompt_chars must be at least 4000")
        if self.max_expansion_blocks_per_side > 8:
            raise DraftStructureError(
                "max_expansion_blocks_per_side may not exceed 8"
            )
        if self.max_expansion_requests_per_document > 10:
            raise DraftStructureError(
                "max_expansion_requests_per_document may not exceed 10"
            )
        if self.max_global_candidates_per_pack > 64:
            raise DraftStructureError(
                "max_global_candidates_per_pack may not exceed 64"
            )
        if self.global_neighbor_units > 3:
            raise DraftStructureError("global_neighbor_units may not exceed 3")


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_without_integrity(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key != "integrity"
    }


def _seal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(payload)
    body = _canonical_json(sealed)
    sealed["integrity"] = {
        "payload_sha256": canonical_json_sha256(sealed),
        "serialized_char_count": len(body),
        "estimated_token_count": math.ceil(len(body) / 4),
    }
    return sealed


def _validate_seal(payload: dict[str, Any], *, owner: str) -> None:
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict):
        raise DraftStructureError(f"{owner}.integrity must be an object")
    body = _payload_without_integrity(payload)
    serialized = _canonical_json(body)
    if integrity.get("payload_sha256") != canonical_json_sha256(body):
        raise DraftStructureError(f"{owner} payload hash differs")
    if integrity.get("serialized_char_count") != len(serialized):
        raise DraftStructureError(f"{owner} serialized_char_count differs")
    if integrity.get("estimated_token_count") != math.ceil(len(serialized) / 4):
        raise DraftStructureError(f"{owner} estimated_token_count differs")


def _flatten_document(
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list):
        raise DraftStructureError("document.chapters must be a list")
    blocks: list[dict[str, Any]] = []
    block_to_chapter: dict[str, str] = {}
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise DraftStructureError("document chapter must be an object")
        chapter_id = str(chapter.get("chapter_id") or "")
        if not chapter_id:
            raise DraftStructureError("document chapter_id is required")
        chapter_blocks = chapter.get("blocks")
        if not isinstance(chapter_blocks, list):
            raise DraftStructureError("document chapter blocks must be a list")
        for block in chapter_blocks:
            if not isinstance(block, dict):
                raise DraftStructureError("document block must be an object")
            block_id = str(block.get("block_id") or "")
            if not block_id or block_id in block_to_chapter:
                raise DraftStructureError("document block_id must be unique")
            blocks.append(block)
            block_to_chapter[block_id] = chapter_id
    return blocks, block_to_chapter


def _validate_report_document_binding(
    report: dict[str, Any],
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    validate_draft_structure_report_shape(report)
    if report.get("editable") is not True:
        raise DraftStructureError("draft structure report is not editable")
    inputs = report.get("inputs")
    if not isinstance(inputs, dict):
        raise DraftStructureError("draft structure report inputs are missing")
    document_identity = inputs.get("document")
    if not isinstance(document_identity, dict):
        raise DraftStructureError("draft structure report document identity is missing")
    if document_identity.get("sha256") != canonical_json_sha256(document):
        raise DraftStructureError("document differs from draft structure report")
    integrity = report.get("integrity")
    if not isinstance(integrity, dict):
        raise DraftStructureError("draft structure report integrity is missing")
    if integrity.get("payload_sha256") != canonical_json_sha256(
        _payload_without_integrity(report)
    ):
        raise DraftStructureError("draft structure report payload hash differs")

    blocks, block_to_chapter = _flatten_document(document)
    by_block = {str(block["block_id"]): block for block in blocks}
    report_units = report.get("units")
    if not isinstance(report_units, list):
        raise DraftStructureError("draft structure report units must be a list")
    report_order = [
        str(block_id)
        for unit in report_units
        for block_id in (unit.get("block_ids") or [])
    ]
    document_order = [str(block["block_id"]) for block in blocks]
    if report_order != document_order:
        raise DraftStructureError(
            "draft structure report does not exact-cover document blocks"
        )
    return blocks, by_block, block_to_chapter


def _preview_text(block: dict[str, Any], *, limit: int) -> dict[str, Any]:
    text = str(block.get("clean_text") or block.get("source_text") or "")
    block_type = str(block.get("block_type") or "")
    effective_limit = max(limit, 1_500) if block_type == "heading" else limit
    if len(text) <= effective_limit:
        preview = text
        truncated = False
    else:
        left = effective_limit // 2
        right = effective_limit - left
        preview = f"{text[:left]}\n<TRUNCATED>\n{text[-right:]}"
        truncated = True
    return {
        "block_id": str(block.get("block_id") or ""),
        "block_type": block_type,
        "order_index": block.get("order_index"),
        "page_ids": copy.deepcopy(block.get("page_ids") or []),
        "quality_flags": sorted(str(flag) for flag in block.get("quality_flags") or []),
        "text_preview": preview,
        "text_char_count": len(text),
        "truncated": truncated,
    }


def _outline(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for unit in report["units"]:
        block_ids = [str(item) for item in unit["block_ids"]]
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "chapter_id": unit["chapter_id"],
                "order_index": unit["order_index"],
                "title": unit["title"],
                "first_block_id": block_ids[0] if block_ids else None,
                "last_block_id": block_ids[-1] if block_ids else None,
                "block_count": len(block_ids),
                "role": unit["role"],
                "translation_policy": unit["translation_policy"],
                "confidence": unit["confidence"],
                "issue_codes": copy.deepcopy(unit["issue_codes"]),
            }
        )
    return rows


def _focus_unit_ids(
    report: dict[str, Any],
    *,
    include_all_units: bool,
) -> list[str]:
    units = report["units"]
    if include_all_units:
        return [str(unit["unit_id"]) for unit in units]
    issue_targets = {
        str(issue["target_id"])
        for issue in report.get("issues") or []
        if issue.get("scope") == "unit"
    }
    issue_indices = {
        index
        for index, unit in enumerate(units)
        if str(unit["unit_id"]) in issue_targets or unit.get("issue_codes")
    }
    focus_indices = {
        neighbor
        for index in issue_indices
        for neighbor in (index - 1, index, index + 1)
        if 0 <= neighbor < len(units)
    }
    return [str(units[index]["unit_id"]) for index in sorted(focus_indices)]


def _boundary_id(payload: dict[str, Any]) -> str:
    return f"bd_{canonical_json_sha256(payload)[:20]}"


def _boundary_cases(
    report: dict[str, Any],
    document: dict[str, Any],
    focus_unit_ids: list[str],
    *,
    budget: StructureContextBudget,
) -> list[dict[str, Any]]:
    blocks, by_block, _block_to_chapter = _validate_report_document_binding(
        report,
        document,
    )
    block_order = {str(block["block_id"]): index for index, block in enumerate(blocks)}
    units = report["units"]
    unit_order = {str(unit["unit_id"]): index for index, unit in enumerate(units)}
    focus = set(focus_unit_ids)
    candidates: dict[str, dict[str, Any]] = {}

    def add_case(
        *,
        kind: str,
        at_block_id: str,
        left_unit_id: str | None,
        right_unit_id: str | None,
        target_unit_id: str,
        signals: list[str],
    ) -> None:
        pivot_index = block_order[at_block_id]
        left_start = max(0, pivot_index - budget.left_blocks)
        right_stop = min(len(blocks), pivot_index + 1 + budget.right_blocks)
        identity = {
            "kind": kind,
            "at_block_id": at_block_id,
            "left_unit_id": left_unit_id,
            "right_unit_id": right_unit_id,
            "target_unit_id": target_unit_id,
        }
        boundary_id = _boundary_id(identity)
        candidates[boundary_id] = {
            "boundary_id": boundary_id,
            **identity,
            "signals": sorted(set(signals)),
            "left_context": [
                _preview_text(block, limit=budget.block_preview_chars)
                for block in blocks[left_start:pivot_index]
            ],
            "pivot": _preview_text(
                by_block[at_block_id],
                limit=budget.block_preview_chars,
            ),
            "right_context": [
                _preview_text(block, limit=budget.block_preview_chars)
                for block in blocks[pivot_index + 1 : right_stop]
            ],
        }

    for unit in units:
        unit_id = str(unit["unit_id"])
        if unit_id not in focus:
            continue
        index = unit_order[unit_id]
        block_ids = [str(item) for item in unit["block_ids"]]
        issue_signals = [str(item) for item in unit.get("issue_codes") or []]
        if index > 0 and block_ids:
            add_case(
                kind="unit_start",
                at_block_id=block_ids[0],
                left_unit_id=str(units[index - 1]["unit_id"]),
                right_unit_id=unit_id,
                target_unit_id=unit_id,
                signals=["existing_unit_boundary", *issue_signals],
            )
        if index + 1 < len(units):
            next_block_ids = [str(item) for item in units[index + 1]["block_ids"]]
            if next_block_ids:
                next_unit_id = str(units[index + 1]["unit_id"])
                add_case(
                    kind="unit_start",
                    at_block_id=next_block_ids[0],
                    left_unit_id=unit_id,
                    right_unit_id=next_unit_id,
                    target_unit_id=(
                        next_unit_id if next_unit_id in focus else unit_id
                    ),
                    signals=["existing_unit_boundary", *issue_signals],
                )
        for block_id in block_ids[1:]:
            block = by_block[block_id]
            if block.get("block_type") == "heading":
                add_case(
                    kind="internal_heading",
                    at_block_id=block_id,
                    left_unit_id=unit_id,
                    right_unit_id=None,
                    target_unit_id=unit_id,
                    signals=["internal_heading", *issue_signals],
                )
    return sorted(
        candidates.values(),
        key=lambda row: (
            block_order[row["at_block_id"]],
            row["kind"],
            row["boundary_id"],
        ),
    )


def _allowed_scope(
    focus_unit_ids: list[str],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    split_boundaries = sorted(
        [
            {
                "unit_id": row["target_unit_id"],
                "at_block_id": row["at_block_id"],
            }
            for row in cases
            if row["kind"] == "internal_heading"
        ],
        key=lambda row: (row["unit_id"], row["at_block_id"]),
    )
    focus = set(focus_unit_ids)
    merge_boundaries = sorted(
        [
            {
                "left_unit_id": row["left_unit_id"],
                "right_unit_id": row["right_unit_id"],
            }
            for row in cases
            if row["kind"] == "unit_start"
            and row["left_unit_id"] is not None
            and row["right_unit_id"] is not None
            and row["left_unit_id"] in focus
            and row["right_unit_id"] in focus
        ],
        key=lambda row: (row["left_unit_id"], row["right_unit_id"]),
    )
    return {
        "update_unit_ids": sorted(focus_unit_ids),
        "split_boundaries": split_boundaries,
        "merge_boundaries": merge_boundaries,
    }


def _response_contract(
    schema_dialect: str = BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
) -> dict[str, Any]:
    if schema_dialect not in _BOUNDARY_REPAIR_SCHEMA_DIALECTS:
        raise DraftStructureError(
            "boundary-repair response-contract dialect is unsupported"
        )
    contract = {
        "schema_version": RESPONSE_VERSION,
        "top_level_fields": [
            "schema_version",
            "report_sha256",
            "context_pack_sha256",
            "actions",
            "abstentions",
        ],
        "action_shapes": {
            "update_unit": {
                "action_type": "update_unit",
                "unit_id": "string",
                "new_title": "string|null",
                "classification": "translate|preserve|exclude|review|null",
            },
            "split_unit": {
                "action_type": "split_unit",
                "unit_id": "string",
                "at_block_id": "string",
                "left_title": "string",
                "right_title": "string",
                "left_classification": "translate|preserve|exclude|review",
                "right_classification": "translate|preserve|exclude|review",
            },
            "merge_adjacent_units": {
                "action_type": "merge_adjacent_units",
                "left_unit_id": "string",
                "right_unit_id": "string",
                "new_title": "string",
                "classification": "translate|preserve|exclude|review",
            },
        },
        "abstention_shape": {
            "unit_id": "string",
            "reason": sorted(ABSTENTION_REASONS),
        },
        "additional_fields_allowed": False,
    }
    if schema_dialect == BOUNDARY_REPAIR_SCHEMA_DIALECT_V2:
        contract["contract_dialect"] = schema_dialect
        contract["coverage_policy"] = {
            "target_set": "focus_unit_ids",
            "exactly_once_across": ["actions", "abstentions"],
            "actions_and_abstentions_are_mutually_exclusive": True,
            "omissions_allowed": False,
            "duplicates_allowed": False,
        }
    return contract


def _pack_payload(
    report: dict[str, Any],
    document: dict[str, Any],
    focus_unit_ids: list[str],
    *,
    batch_index: int,
    batch_count: int,
    budget: StructureContextBudget,
    response_contract_dialect: str,
) -> dict[str, Any]:
    cases = _boundary_cases(
        report,
        document,
        focus_unit_ids,
        budget=budget,
    )
    blocks, _by_block, _block_to_chapter = _validate_report_document_binding(
        report,
        document,
    )
    metadata = document.get("metadata") or {}
    return {
        "schema_version": CONTEXT_PACK_VERSION,
        "doc_id": report["doc_id"],
        "report_sha256": report["integrity"]["payload_sha256"],
        "document_sha256": report["inputs"]["document"]["sha256"],
        "batch_index": batch_index,
        "batch_count": batch_count,
        "context_policy": {
            "schema_version": CONTEXT_POLICY_VERSION,
            "max_prompt_chars": budget.max_prompt_chars,
            "max_focus_units_per_pack": budget.max_focus_units_per_pack,
            "left_blocks": budget.left_blocks,
            "right_blocks": budget.right_blocks,
            "block_preview_chars": budget.block_preview_chars,
            "headings_are_not_truncated_below_chars": 1_500,
            "max_expansion_requests_per_document": (
                budget.max_expansion_requests_per_document
            ),
        },
        "document_summary": {
            "source_format": str(metadata.get("source_format") or ""),
            "title": str(metadata.get("title") or ""),
            "unit_count": len(report["units"]),
            "block_count": len(blocks),
            "issue_count": len(report.get("issues") or []),
        },
        "outline": _outline(report),
        "focus_unit_ids": sorted(focus_unit_ids),
        "boundary_cases": cases,
        "allowed_scope": _allowed_scope(focus_unit_ids, cases),
        "response_contract": _response_contract(response_contract_dialect),
    }


_PROMPT_PREAMBLE_V1 = """You are a book-neutral document-structure assistant.
Use only the supplied structural evidence. Do not rely on knowledge of a named
book or invent text, blocks, IDs, titles, or boundaries. A split is permitted
only at an exposed existing block boundary. A merge is permitted only for an
exposed adjacent pair. When evidence is insufficient or conflicting, abstain.
Return one strict JSON object with schema_version, report_sha256,
context_pack_sha256, actions, and abstentions. Actions must use only the closed
forms update_unit, split_unit, or merge_adjacent_units shown by allowed_scope.
Every focus_unit_id must be covered by exactly one action or one abstention.
This response is advisory: code and a human approval gate make the decision.
CONTEXT_PACK_JSON:
"""


_PROMPT_PREAMBLE_V2 = """You are a book-neutral document-structure assistant.
Use only the supplied structural evidence. Do not rely on knowledge of a named
book or invent text, blocks, IDs, titles, or boundaries. A split is permitted
only at an exposed existing block boundary. A merge is permitted only for an
exposed adjacent pair. When evidence is insufficient or conflicting, abstain.
Return one strict JSON object with schema_version, report_sha256,
context_pack_sha256, actions, and abstentions. Actions must use only the closed
forms update_unit, split_unit, or merge_adjacent_units shown by allowed_scope.
Actions and abstentions are mutually exclusive. If an action targets a focus
unit, do not also abstain on that unit. Before returning JSON, perform this final
coverage self-check: action target unit IDs and abstention unit IDs are disjoint,
contain no duplicates, omit nothing, and together cover every focus_unit_id
exactly once. This response is advisory: code and a human approval gate make the
decision.
CONTEXT_PACK_JSON:
"""


def _prompt_preamble(schema_dialect: str) -> str:
    if schema_dialect == BOUNDARY_REPAIR_SCHEMA_DIALECT_V1:
        return _PROMPT_PREAMBLE_V1
    if schema_dialect == BOUNDARY_REPAIR_SCHEMA_DIALECT_V2:
        return _PROMPT_PREAMBLE_V2
    raise DraftStructureError(
        "boundary-repair response-contract dialect is unsupported"
    )


BOUNDARY_REPAIR_PROMPT_ID = "input_boundary_repair_prompt_v1"
BOUNDARY_REPAIR_RESPONSE_SCHEMA_ID = "input_boundary_repair_schema_v1"
BOUNDARY_REPAIR_VALIDATOR_ID = "input_boundary_repair_local_validator_v1"
BOUNDARY_REPAIR_SEMANTIC_EXTENSION_ID = (
    "input_boundary_repair_semantic_extension_v1"
)


def boundary_repair_contract_identities(
    schema_dialect: str = BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
) -> dict[str, dict[str, str]]:
    """Return content identities for the pipeline-owned semantic contract."""

    response_contract = _response_contract(schema_dialect)
    revision = (
        "v1"
        if schema_dialect == BOUNDARY_REPAIR_SCHEMA_DIALECT_V1
        else "v2"
    )
    prompt_preamble = _prompt_preamble(schema_dialect)
    validator_contract = {
        "schema_version": f"input_boundary_repair_validator_contract_{revision}",
        "parser": inspect.getsource(parse_structure_response_json),
        "semantic_validator": inspect.getsource(validate_structure_response),
        "plan_builder": inspect.getsource(build_correction_plan),
        "response_contract_sha256": canonical_json_sha256(response_contract),
        "authority": "proposal_only_human_approval_required",
    }
    semantic_extension = {
        "schema_version": f"input_boundary_repair_semantic_extension_{revision}",
        "context_pack_schema": CONTEXT_PACK_VERSION,
        "context_policy_schema": CONTEXT_POLICY_VERSION,
        "response_schema": RESPONSE_VERSION,
        "allowed_actions": sorted(_ACTION_FIELDS),
        "abstention_reasons": sorted(ABSTENTION_REASONS),
        "canonical_effect": "none",
    }
    if schema_dialect == BOUNDARY_REPAIR_SCHEMA_DIALECT_V2:
        semantic_extension["response_contract_dialect"] = schema_dialect
    return {
        "prompt": {
            "id": BOUNDARY_REPAIR_PROMPT_ID,
            "revision": revision,
            "sha256": canonical_json_sha256(prompt_preamble),
        },
        "response_schema": {
            "id": BOUNDARY_REPAIR_RESPONSE_SCHEMA_ID,
            "revision": revision,
            "sha256": canonical_json_sha256(response_contract),
        },
        "validator": {
            "id": BOUNDARY_REPAIR_VALIDATOR_ID,
            "revision": revision,
            "sha256": canonical_json_sha256(validator_contract),
        },
        "semantic_extension": {
            "id": BOUNDARY_REPAIR_SEMANTIC_EXTENSION_ID,
            "schema_version": revision,
            "sha256": canonical_json_sha256(semantic_extension),
        },
    }


def render_structure_prompt(
    context_pack: dict[str, Any],
    *,
    response_contract_dialect: str = BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
) -> str:
    _validate_seal(context_pack, owner="context pack")
    if context_pack.get("response_contract") != _response_contract(
        response_contract_dialect
    ):
        raise DraftStructureError(
            "context pack response contract differs from requested dialect"
        )
    return (
        f"{_prompt_preamble(response_contract_dialect)}"
        f"{_canonical_json(context_pack)}"
    )


def parse_structure_response_json(raw_response: str) -> dict[str, Any]:
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise DraftStructureError("LLM raw response must be a non-empty string")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DraftStructureError(f"LLM response repeats JSON key: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw_response, object_pairs_hook=reject_duplicate_keys)
    except DraftStructureError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DraftStructureError("LLM response is not one strict JSON value") from exc
    if not isinstance(parsed, dict):
        raise DraftStructureError("LLM response JSON must be an object")
    return parsed


def build_structure_context_packs(
    report: dict[str, Any],
    document: dict[str, Any],
    *,
    budget: StructureContextBudget | None = None,
    include_all_units: bool = False,
    focus_unit_ids: list[str] | None = None,
    response_contract_dialect: str = BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
) -> list[dict[str, Any]]:
    _response_contract(response_contract_dialect)
    active_budget = budget or StructureContextBudget()
    active_budget.validate()
    _validate_report_document_binding(report, document)
    if include_all_units and focus_unit_ids is not None:
        raise DraftStructureError(
            "include_all_units and focus_unit_ids may not be combined"
        )
    if focus_unit_ids is None:
        focus = _focus_unit_ids(report, include_all_units=include_all_units)
    else:
        requested = [str(unit_id) for unit_id in focus_unit_ids]
        if len(requested) != len(set(requested)):
            raise DraftStructureError("focus_unit_ids must not contain duplicates")
        requested_set = set(requested)
        report_order = [
            str(unit["unit_id"])
            for unit in report["units"]
            if str(unit["unit_id"]) in requested_set
        ]
        if set(report_order) != requested_set:
            raise DraftStructureError("focus_unit_ids contains an unknown unit")
        focus = report_order
    if not focus:
        return []
    chunks: list[list[str]] = []
    current: list[str] = []
    for unit_id in focus:
        proposed = [*current, unit_id]
        provisional = _seal_payload(
            _pack_payload(
                report,
                document,
                proposed,
                batch_index=1,
                batch_count=1,
                budget=active_budget,
                response_contract_dialect=response_contract_dialect,
            )
        )
        fits = (
            len(proposed) <= active_budget.max_focus_units_per_pack
            and len(
                render_structure_prompt(
                    provisional,
                    response_contract_dialect=response_contract_dialect,
                )
            )
            <= active_budget.max_prompt_chars
        )
        if fits:
            current = proposed
            continue
        if not current:
            raise DraftStructureError(
                "one structure focus unit exceeds max_prompt_chars; "
                "increase the explicit budget or review manually"
            )
        chunks.append(current)
        current = [unit_id]
        single = _seal_payload(
            _pack_payload(
                report,
                document,
                current,
                batch_index=1,
                batch_count=1,
                budget=active_budget,
                response_contract_dialect=response_contract_dialect,
            )
        )
        if (
            len(
                render_structure_prompt(
                    single,
                    response_contract_dialect=response_contract_dialect,
                )
            )
            > active_budget.max_prompt_chars
        ):
            raise DraftStructureError(
                "one structure focus unit exceeds max_prompt_chars; "
                "increase the explicit budget or review manually"
            )
    if current:
        chunks.append(current)
    packs: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        pack = _seal_payload(
            _pack_payload(
                report,
                document,
                chunk,
                batch_index=index,
                batch_count=len(chunks),
                budget=active_budget,
                response_contract_dialect=response_contract_dialect,
            )
        )
        prompt = render_structure_prompt(
            pack,
            response_contract_dialect=response_contract_dialect,
        )
        if len(prompt) > active_budget.max_prompt_chars:
            raise DraftStructureError(
                f"structure context pack {index} exceeds max_prompt_chars; "
                "reduce focus units or review manually"
            )
        packs.append(pack)
    return packs


_GLOBAL_PACK_FIELDS = {
    "schema_version",
    "doc_id",
    "report_sha256",
    "skeleton_sha256",
    "document_sha256",
    "batch_index",
    "batch_count",
    "document_summary",
    "skeleton_policy",
    "context_budget",
    "assigned_candidate_ids",
    "candidates",
    "outline_context",
    "navigation_context",
    "local_context",
    "allowed_scope",
    "response_contract",
    "integrity",
}

_HIERARCHY_PACK_FIELDS = {
    "schema_version",
    "doc_id",
    "inputs",
    "report_sha256",
    "skeleton_sha256",
    "policy_sha256",
    "context_budget",
    "outline",
    "navigation",
    "allowed_unit_ids",
    "response_contract",
    "integrity",
}


def _global_outline_context(
    skeleton: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    neighbor_units: int,
) -> list[dict[str, Any]]:
    outline = skeleton["outline"]
    order_by_unit = {
        str(row["unit_id"]): index for index, row in enumerate(outline)
    }
    selected: set[int] = set()
    for candidate in candidates:
        for unit_id in candidate["unit_ids"]:
            pivot = order_by_unit.get(str(unit_id))
            if pivot is None:
                raise DraftStructureError(
                    "global candidate references an unknown outline unit"
                )
            start = max(0, pivot - neighbor_units)
            stop = min(len(outline), pivot + neighbor_units + 1)
            selected.update(range(start, stop))
    if not selected and any(
        row["candidate_kind"] == "signal_starvation" for row in candidates
    ):
        selected.update(range(len(outline)))
    return [copy.deepcopy(outline[index]) for index in sorted(selected)]


def _global_navigation_context(
    skeleton: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected_entry_ids: set[str] = set()
    selected_titles: set[str] = set()
    selected_blocks: set[str] = set()
    include_all = False
    for candidate in candidates:
        source_ref = str(candidate["source_ref"])
        if source_ref.startswith("navigation:"):
            selected_entry_ids.add(source_ref.split(":", 1)[1])
        if candidate["candidate_kind"] == "duplicate_title_group":
            selected_titles.add(str(candidate["title"] or ""))
        if candidate["candidate_kind"] == "signal_starvation":
            include_all = True
        selected_blocks.update(str(item) for item in candidate["block_ids"])
    rows: list[dict[str, Any]] = []
    for row in skeleton["navigation"]:
        candidate_blocks = {str(item) for item in row["candidate_block_ids"]}
        if (
            include_all
            or str(row["entry_id"]) in selected_entry_ids
            or str(row["normalized_title"]) in selected_titles
            or bool(candidate_blocks & selected_blocks)
        ):
            rows.append(copy.deepcopy(row))
    return rows


def _global_local_context(
    document: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    budget: StructureContextBudget,
) -> list[dict[str, Any]]:
    blocks, _block_to_chapter = _flatten_document(document)
    order = {str(block["block_id"]): index for index, block in enumerate(blocks)}
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        at_block_id = candidate["at_block_id"]
        if at_block_id is None:
            continue
        pivot = order.get(str(at_block_id))
        if pivot is None:
            raise DraftStructureError(
                "global candidate boundary block is absent from document"
            )
        start = max(0, pivot - budget.left_blocks)
        stop = min(len(blocks), pivot + budget.right_blocks + 1)
        rows.append(
            {
                "candidate_id": candidate["candidate_id"],
                "at_block_id": at_block_id,
                "left_blocks": [
                    _preview_text(block, limit=budget.block_preview_chars)
                    for block in blocks[start:pivot]
                ],
                "pivot": _preview_text(
                    blocks[pivot], limit=budget.block_preview_chars
                ),
                "right_blocks": [
                    _preview_text(block, limit=budget.block_preview_chars)
                    for block in blocks[pivot + 1 : stop]
                ],
            }
        )
    return rows


def _global_allowed_scope(
    skeleton: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    outline = skeleton["outline"]
    order_by_unit = {
        str(row["unit_id"]): index for index, row in enumerate(outline)
    }
    first_by_unit = {
        str(row["unit_id"]): row["first_block_id"] for row in outline
    }
    rows: list[dict[str, Any]] = []
    split_kinds = {
        "internal_heading",
        "mechanical_text_boundary",
        "navigation_entry",
        "numbering_restart",
    }
    for candidate in candidates:
        unit_ids = [str(item) for item in candidate["unit_ids"]]
        update_unit_ids: list[str] = []
        split_boundaries: list[dict[str, str]] = []
        merge_boundaries: list[dict[str, str]] = []
        if len(unit_ids) == 1 and candidate["candidate_kind"] in split_kinds:
            update_unit_ids = list(unit_ids)
            at_block_id = candidate["at_block_id"]
            if (
                at_block_id is not None
                and at_block_id != first_by_unit[unit_ids[0]]
            ):
                split_boundaries.append(
                    {
                        "unit_id": unit_ids[0],
                        "at_block_id": str(at_block_id),
                    }
                )
        if (
            candidate["candidate_kind"] == "existing_unit_boundary"
            and len(unit_ids) == 2
            and order_by_unit[unit_ids[1]] == order_by_unit[unit_ids[0]] + 1
        ):
            merge_boundaries.append(
                {
                    "left_unit_id": unit_ids[0],
                    "right_unit_id": unit_ids[1],
                }
            )
        rows.append(
            {
                "candidate_id": candidate["candidate_id"],
                "update_unit_ids": update_unit_ids,
                "split_boundaries": split_boundaries,
                "merge_boundaries": merge_boundaries,
            }
        )
    return rows


def _global_response_contract() -> dict[str, Any]:
    return {
        "schema_version": GLOBAL_RESPONSE_VERSION,
        "coverage_rule": "each_assigned_candidate_exactly_once",
        "action_wrapper_fields": ["candidate_id", "proposal"],
        "abstention_fields": ["candidate_id", "reason"],
        "allowed_action_types": sorted(_ACTION_FIELDS),
        "allowed_abstention_reasons": sorted(ABSTENTION_REASONS),
    }


def _global_pack_payload(
    report: dict[str, Any],
    document: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    batch_index: int,
    batch_count: int,
    budget: StructureContextBudget,
) -> dict[str, Any]:
    skeleton = report["global_skeleton"]
    metadata = document.get("metadata") or {}
    return {
        "schema_version": GLOBAL_CONTEXT_PACK_VERSION,
        "doc_id": report["doc_id"],
        "report_sha256": report["integrity"]["payload_sha256"],
        "skeleton_sha256": skeleton["integrity"]["payload_sha256"],
        "document_sha256": canonical_json_sha256(document),
        "batch_index": batch_index,
        "batch_count": batch_count,
        "document_summary": {
            "source_format": str(metadata.get("source_format") or ""),
            "title": str(metadata.get("title") or ""),
            **copy.deepcopy(skeleton["statistics"]),
        },
        "skeleton_policy": copy.deepcopy(skeleton["policy"]),
        "context_budget": {
            "left_blocks": budget.left_blocks,
            "right_blocks": budget.right_blocks,
            "block_preview_chars": budget.block_preview_chars,
            "global_neighbor_units": budget.global_neighbor_units,
            "max_global_candidates_per_pack": (
                budget.max_global_candidates_per_pack
            ),
        },
        "assigned_candidate_ids": [
            str(candidate["candidate_id"]) for candidate in candidates
        ],
        "candidates": copy.deepcopy(candidates),
        "outline_context": _global_outline_context(
            skeleton,
            candidates,
            neighbor_units=budget.global_neighbor_units,
        ),
        "navigation_context": _global_navigation_context(skeleton, candidates),
        "local_context": _global_local_context(
            document, candidates, budget=budget
        ),
        "allowed_scope": _global_allowed_scope(skeleton, candidates),
        "response_contract": _global_response_contract(),
    }


_GLOBAL_PROMPT_PREAMBLE = """You are a book-neutral structure-correction assistant.
The complete candidate inventory was computed by code before this bounded pack.
Use only the assigned candidates and supplied source evidence. Never invent,
reorder, delete, or rewrite source blocks. A split may use only an exposed
existing block boundary; a merge may use only an exposed adjacent unit pair.
Navigation and mechanical signals are suspects, not truth. Duplicate, missing,
or ambiguous navigation remains unresolved unless evidence in this pack is
sufficient. For every assigned candidate, return exactly one candidate-scoped
action or one abstention. Return strict JSON only, following response_contract.
This is advisory and has no canonical effect until deterministic validation and
human approval.
GLOBAL_CONTEXT_PACK_JSON:
"""


def render_global_structure_prompt(
    context_pack: dict[str, Any],
    *,
    report: dict[str, Any],
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    package_root: str | Path | None = None,
) -> str:
    validate_global_structure_context_pack(
        context_pack,
        report=report,
        document=document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        package_root=package_root,
    )
    return _render_global_structure_prompt_unchecked(context_pack)


def _render_global_structure_prompt_unchecked(
    context_pack: dict[str, Any],
) -> str:
    return f"{_GLOBAL_PROMPT_PREAMBLE}{_canonical_json(context_pack)}"


def validate_global_structure_context_pack(
    context_pack: dict[str, Any],
    *,
    report: dict[str, Any] | None = None,
    document: dict[str, Any] | None = None,
    structure_manifest: dict[str, Any] | None = None,
    asset_manifest: dict[str, Any] | None = None,
    admitted_projection: dict[str, Any] | None = None,
    package_root: str | Path | None = None,
) -> None:
    if not isinstance(context_pack, dict):
        raise DraftStructureError("global context pack must be an object")
    _require_exact_fields(
        context_pack, _GLOBAL_PACK_FIELDS, owner="global context pack"
    )
    if context_pack.get("schema_version") != GLOBAL_CONTEXT_PACK_VERSION:
        raise DraftStructureError("global context pack version differs")
    _validate_seal(context_pack, owner="global context pack")
    assigned = context_pack.get("assigned_candidate_ids")
    candidates = context_pack.get("candidates")
    scopes = context_pack.get("allowed_scope")
    if not isinstance(assigned, list) or not isinstance(candidates, list):
        raise DraftStructureError("global context candidates must be lists")
    if not isinstance(scopes, list):
        raise DraftStructureError("global context allowed_scope must be a list")
    actual = [str(row.get("candidate_id") or "") for row in candidates]
    scope_ids = [str(row.get("candidate_id") or "") for row in scopes]
    if assigned != actual or assigned != scope_ids:
        raise DraftStructureError("global context candidate coverage differs")
    if len(assigned) != len(set(assigned)):
        raise DraftStructureError("global context repeats candidate_id")
    if report is not None:
        authoritative_inputs = (
            document,
            structure_manifest,
            asset_manifest,
            admitted_projection,
        )
        if any(value is None for value in authoritative_inputs):
            raise DraftStructureError(
                "global context validation requires the complete source package"
            )
        if context_pack["report_sha256"] != report["integrity"]["payload_sha256"]:
            raise DraftStructureError("global context report identity differs")
        skeleton = report.get("global_skeleton")
        validate_global_structure_skeleton(
            skeleton,
            authoritative_document=document,
            authoritative_structure_manifest=structure_manifest,
            authoritative_asset_manifest=asset_manifest,
            authoritative_admitted_projection=admitted_projection,
            package_root=package_root,
        )
        if context_pack["skeleton_sha256"] != skeleton["integrity"]["payload_sha256"]:
            raise DraftStructureError("global context skeleton identity differs")
        by_candidate = {
            str(row["candidate_id"]): row for row in skeleton["candidates"]
        }
        if any(candidate_id not in by_candidate for candidate_id in assigned):
            raise DraftStructureError("global context references unknown candidate")
        expected_candidates = [
            by_candidate[candidate_id] for candidate_id in assigned
        ]
        skeleton_order = [
            str(row["candidate_id"])
            for row in skeleton["candidates"]
            if str(row["candidate_id"]) in set(assigned)
        ]
        if assigned != skeleton_order or candidates != expected_candidates:
            raise DraftStructureError("global context candidate payload differs")
        budget_payload = context_pack.get("context_budget")
        if not isinstance(budget_payload, dict):
            raise DraftStructureError("global context budget must be an object")
        expected_budget_fields = {
            "left_blocks",
            "right_blocks",
            "block_preview_chars",
            "global_neighbor_units",
            "max_global_candidates_per_pack",
        }
        _require_exact_fields(
            budget_payload,
            expected_budget_fields,
            owner="global context budget",
        )
        validation_budget = StructureContextBudget(
            left_blocks=budget_payload["left_blocks"],
            right_blocks=budget_payload["right_blocks"],
            block_preview_chars=budget_payload["block_preview_chars"],
            global_neighbor_units=budget_payload["global_neighbor_units"],
            max_global_candidates_per_pack=(
                budget_payload["max_global_candidates_per_pack"]
            ),
        )
        validation_budget.validate()
        if context_pack["outline_context"] != _global_outline_context(
            skeleton,
            expected_candidates,
            neighbor_units=validation_budget.global_neighbor_units,
        ):
            raise DraftStructureError("global context outline evidence differs")
        if context_pack["navigation_context"] != _global_navigation_context(
            skeleton, expected_candidates
        ):
            raise DraftStructureError("global context navigation evidence differs")
        if context_pack["allowed_scope"] != _global_allowed_scope(
            skeleton, expected_candidates
        ):
            raise DraftStructureError("global context allowed scope differs")
        if context_pack["skeleton_policy"] != skeleton["policy"]:
            raise DraftStructureError("global context skeleton policy differs")
        if canonical_json_sha256(document) != context_pack["document_sha256"]:
            raise DraftStructureError("global context document identity differs")
        if context_pack["local_context"] != _global_local_context(
            document, expected_candidates, budget=validation_budget
        ):
            raise DraftStructureError("global context local evidence differs")


def build_global_structure_context_packs(
    report: dict[str, Any],
    document: dict[str, Any],
    *,
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    package_root: str | Path | None = None,
    budget: StructureContextBudget | None = None,
) -> list[dict[str, Any]]:
    active_budget = budget or StructureContextBudget()
    active_budget.validate()
    _validate_report_document_binding(report, document)
    skeleton = report.get("global_skeleton")
    validate_global_structure_skeleton(
        skeleton,
        authoritative_document=document,
        authoritative_structure_manifest=structure_manifest,
        authoritative_asset_manifest=asset_manifest,
        authoritative_admitted_projection=admitted_projection,
        package_root=package_root,
    )
    if skeleton["doc_id"] != report.get("doc_id"):
        raise DraftStructureError("global skeleton and report doc_id differ")
    if skeleton["inputs"]["document"] != report["inputs"]["document"]:
        raise DraftStructureError("global skeleton and report document differ")
    candidates = skeleton["candidates"]
    if not candidates:
        return []
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for candidate in candidates:
        proposed = [*current, candidate]
        provisional = _seal_payload(
            _global_pack_payload(
                report,
                document,
                proposed,
                batch_index=1,
                batch_count=1,
                budget=active_budget,
            )
        )
        fits = (
            len(proposed) <= active_budget.max_global_candidates_per_pack
            and len(_render_global_structure_prompt_unchecked(provisional))
            <= active_budget.max_prompt_chars
        )
        if fits:
            current = proposed
            continue
        if not current:
            raise DraftStructureError(
                "one global structure candidate exceeds max_prompt_chars; "
                "the candidate remains persisted and requires manual review"
            )
        chunks.append(current)
        current = [candidate]
        single = _seal_payload(
            _global_pack_payload(
                report,
                document,
                current,
                batch_index=1,
                batch_count=1,
                budget=active_budget,
            )
        )
        if (
            len(_render_global_structure_prompt_unchecked(single))
            > active_budget.max_prompt_chars
        ):
            raise DraftStructureError(
                "one global structure candidate exceeds max_prompt_chars; "
                "the candidate remains persisted and requires manual review"
            )
    if current:
        chunks.append(current)
    packs: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        pack = _seal_payload(
            _global_pack_payload(
                report,
                document,
                chunk,
                batch_index=index,
                batch_count=len(chunks),
                budget=active_budget,
            )
        )
        validate_global_structure_context_pack(
            pack,
            report=report,
            document=document,
            structure_manifest=structure_manifest,
            asset_manifest=asset_manifest,
            admitted_projection=admitted_projection,
            package_root=package_root,
        )
        if (
            len(_render_global_structure_prompt_unchecked(pack))
            > active_budget.max_prompt_chars
        ):
            raise DraftStructureError("global structure context pack exceeds budget")
        packs.append(pack)
    assigned = [
        candidate_id
        for pack in packs
        for candidate_id in pack["assigned_candidate_ids"]
    ]
    expected = [str(row["candidate_id"]) for row in candidates]
    if assigned != expected or len(assigned) != len(set(assigned)):
        raise DraftStructureError(
            "global structure packs must exact-cover persisted candidate IDs"
        )
    return packs


def _validate_global_action_scope(
    proposal: dict[str, Any],
    scope: dict[str, Any],
) -> None:
    action_type = proposal.get("action_type")
    if action_type not in _ACTION_FIELDS:
        raise DraftStructureError("global response action_type is unsupported")
    _require_exact_fields(
        proposal,
        _ACTION_FIELDS[str(action_type)],
        owner="global response proposal",
    )
    if action_type == "update_unit":
        if proposal["unit_id"] not in scope["update_unit_ids"]:
            raise DraftStructureError("global update_unit is outside candidate scope")
    elif action_type == "split_unit":
        boundary = {
            "unit_id": proposal["unit_id"],
            "at_block_id": proposal["at_block_id"],
        }
        if boundary not in scope["split_boundaries"]:
            raise DraftStructureError("global split_unit is outside candidate scope")
    else:
        boundary = {
            "left_unit_id": proposal["left_unit_id"],
            "right_unit_id": proposal["right_unit_id"],
        }
        if boundary not in scope["merge_boundaries"]:
            raise DraftStructureError(
                "global merge_adjacent_units is outside candidate scope"
            )


def validate_global_structure_response(
    response: dict[str, Any],
    report: dict[str, Any],
    context_pack: dict[str, Any],
    *,
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    package_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        raise DraftStructureError("global structure response must be an object")
    _require_exact_fields(
        response,
        {
            "schema_version",
            "report_sha256",
            "skeleton_sha256",
            "context_pack_sha256",
            "actions",
            "abstentions",
        },
        owner="global structure response",
    )
    validate_global_structure_context_pack(
        context_pack,
        report=report,
        document=document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        package_root=package_root,
    )
    if response["schema_version"] != GLOBAL_RESPONSE_VERSION:
        raise DraftStructureError("global structure response version differs")
    if response["report_sha256"] != report["integrity"]["payload_sha256"]:
        raise DraftStructureError("global structure response report differs")
    if response["skeleton_sha256"] != context_pack["skeleton_sha256"]:
        raise DraftStructureError("global structure response skeleton differs")
    if response["context_pack_sha256"] != context_pack["integrity"]["payload_sha256"]:
        raise DraftStructureError("global structure response context pack differs")
    actions = response["actions"]
    abstentions = response["abstentions"]
    if not isinstance(actions, list) or not isinstance(abstentions, list):
        raise DraftStructureError("global actions and abstentions must be lists")
    scopes = {str(row["candidate_id"]): row for row in context_pack["allowed_scope"]}
    assigned = set(str(item) for item in context_pack["assigned_candidate_ids"])
    covered: list[str] = []
    prepared: list[dict[str, Any]] = []
    for row in actions:
        if not isinstance(row, dict):
            raise DraftStructureError("global action wrapper must be an object")
        _require_exact_fields(
            row, {"candidate_id", "proposal"}, owner="global action wrapper"
        )
        candidate_id = str(row["candidate_id"])
        if candidate_id not in assigned:
            raise DraftStructureError("global action candidate is not assigned")
        proposal = row["proposal"]
        if not isinstance(proposal, dict):
            raise DraftStructureError("global action proposal must be an object")
        _validate_global_action_scope(proposal, scopes[candidate_id])
        covered.append(candidate_id)
        prepared.append(copy.deepcopy(proposal))
    for row in abstentions:
        if not isinstance(row, dict):
            raise DraftStructureError("global abstention must be an object")
        _require_exact_fields(
            row, {"candidate_id", "reason"}, owner="global abstention"
        )
        candidate_id = str(row["candidate_id"])
        if candidate_id not in assigned:
            raise DraftStructureError("global abstention candidate is not assigned")
        if row["reason"] not in ABSTENTION_REASONS:
            raise DraftStructureError("global abstention reason is unsupported")
        covered.append(candidate_id)
    if sorted(covered) != sorted(assigned) or len(covered) != len(assigned):
        raise DraftStructureError(
            "global response must cover every assigned candidate exactly once"
        )
    return prepared


def run_global_structure_assistant(
    executor: StructureAssistantExecutor,
    report: dict[str, Any],
    document: dict[str, Any],
    *,
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    package_root: str | Path | None = None,
    model_identifier: str,
    budget: StructureContextBudget | None = None,
) -> dict[str, Any]:
    if not isinstance(model_identifier, str) or not model_identifier.strip():
        raise DraftStructureError("model_identifier must be a non-empty string")
    packs = build_global_structure_context_packs(
        report,
        document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        package_root=package_root,
        budget=budget,
    )
    responses: list[dict[str, Any]] = []
    action_specs: list[dict[str, Any]] = []
    for pack in packs:
        prompt = render_global_structure_prompt(
            pack,
            report=report,
            document=document,
            structure_manifest=structure_manifest,
            asset_manifest=asset_manifest,
            admitted_projection=admitted_projection,
            package_root=package_root,
        )
        pack_copy = copy.deepcopy(pack)
        response = executor.complete(prompt, context_pack=pack_copy)
        if pack_copy != pack:
            raise DraftStructureError("global assistant mutated its context pack")
        action_specs.extend(
            validate_global_structure_response(
                response,
                report,
                pack,
                document=document,
                structure_manifest=structure_manifest,
                asset_manifest=asset_manifest,
                admitted_projection=admitted_projection,
                package_root=package_root,
            )
        )
        responses.append(copy.deepcopy(response))
    plan = build_correction_plan(
        report,
        action_specs,
        proposer={"kind": "llm", "identifier": model_identifier.strip()},
    )
    return {
        "context_packs": packs,
        "responses": responses,
        "correction_plan": plan,
    }


def _hierarchy_response_contract() -> dict[str, Any]:
    return {
        "schema_version": HIERARCHY_RESPONSE_VERSION,
        "coverage_rule": "each_allowed_unit_exactly_once",
        "allowed_action_types": ["clear_parent", "set_parent"],
        "set_parent_fields": [
            "action_type",
            "child_unit_id",
            "parent_unit_id",
        ],
        "clear_parent_fields": ["action_type", "child_unit_id"],
        "abstention_fields": ["child_unit_id", "reason"],
        "allowed_abstention_reasons": sorted(ABSTENTION_REASONS),
        "parent_rule": "existing_unit_strictly_before_child",
    }


def _hierarchy_pack_payload(
    report: dict[str, Any],
    *,
    max_prompt_chars: int,
) -> dict[str, Any]:
    skeleton = report["global_skeleton"]
    identities = hierarchy_input_identities(report)
    return {
        "schema_version": HIERARCHY_CONTEXT_PACK_VERSION,
        "doc_id": report["doc_id"],
        "inputs": identities,
        "report_sha256": identities["report"]["sha256"],
        "skeleton_sha256": identities["skeleton"]["sha256"],
        "policy_sha256": identities["policy"]["sha256"],
        "context_budget": {"max_prompt_chars": max_prompt_chars},
        "outline": copy.deepcopy(skeleton["outline"]),
        "navigation": copy.deepcopy(skeleton["navigation"]),
        "allowed_unit_ids": [
            str(row["unit_id"]) for row in skeleton["outline"]
        ],
        "response_contract": _hierarchy_response_contract(),
    }


_HIERARCHY_PROMPT_PREAMBLE = """You are a book-neutral hierarchy proposal assistant.
The complete ordered unit outline and navigation evidence are supplied below.
Use only existing unit IDs. A parent must occur strictly before its child.
Never split, merge, rename, classify, reorder, delete, or invent a unit or block.
For every allowed unit, return exactly one set_parent/clear_parent action or one
abstention. Navigation is evidence, not truth. Abstain when hierarchy evidence
is ambiguous. Return strict JSON only, following response_contract. This output
is proposal-only and cannot alter the canonical package or any live pipeline.
HIERARCHY_CONTEXT_PACK_JSON:
"""


def _render_hierarchy_prompt_unchecked(context_pack: dict[str, Any]) -> str:
    return f"{_HIERARCHY_PROMPT_PREAMBLE}{_canonical_json(context_pack)}"


def build_hierarchy_context_pack(
    report: dict[str, Any],
    document: dict[str, Any],
    *,
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    package_root: str | Path | None = None,
    budget: StructureContextBudget | None = None,
) -> dict[str, Any]:
    active_budget = budget or StructureContextBudget()
    active_budget.validate()
    validate_authoritative_draft_structure_report(
        report,
        document,
        structure_manifest,
        asset_manifest,
        admitted_projection,
        project_state,
        package_root=package_root,
    )
    context_pack = _seal_payload(
        _hierarchy_pack_payload(
            report,
            max_prompt_chars=active_budget.max_prompt_chars,
        )
    )
    validate_hierarchy_context_pack(
        context_pack,
        report=report,
        document=document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        project_state=project_state,
        package_root=package_root,
    )
    if len(_render_hierarchy_prompt_unchecked(context_pack)) > (
        active_budget.max_prompt_chars
    ):
        raise DraftStructureError(
            "complete hierarchy context exceeds max_prompt_chars; "
            "hierarchy context may not be truncated or independently sharded"
        )
    return context_pack


def validate_hierarchy_context_pack(
    context_pack: dict[str, Any],
    *,
    report: dict[str, Any],
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
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
    if not isinstance(context_pack, dict):
        raise DraftStructureError("hierarchy context pack must be an object")
    _require_exact_fields(
        context_pack,
        _HIERARCHY_PACK_FIELDS,
        owner="hierarchy context pack",
    )
    if context_pack.get("schema_version") != HIERARCHY_CONTEXT_PACK_VERSION:
        raise DraftStructureError("hierarchy context pack version differs")
    _validate_seal(context_pack, owner="hierarchy context pack")
    budget = context_pack.get("context_budget")
    if not isinstance(budget, dict):
        raise DraftStructureError("hierarchy context budget must be an object")
    _require_exact_fields(
        budget,
        {"max_prompt_chars"},
        owner="hierarchy context budget",
    )
    max_prompt_chars = budget.get("max_prompt_chars")
    if (
        isinstance(max_prompt_chars, bool)
        or not isinstance(max_prompt_chars, int)
        or max_prompt_chars < 4_000
    ):
        raise DraftStructureError(
            "hierarchy max_prompt_chars must be an integer of at least 4000"
        )
    expected = _seal_payload(
        _hierarchy_pack_payload(report, max_prompt_chars=max_prompt_chars)
    )
    if context_pack != expected:
        raise DraftStructureError(
            "hierarchy context differs from authoritative report and package"
        )
    allowed = context_pack["allowed_unit_ids"]
    if len(allowed) != len(set(allowed)):
        raise DraftStructureError("hierarchy context repeats unit IDs")


def render_hierarchy_prompt(
    context_pack: dict[str, Any],
    *,
    report: dict[str, Any],
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    package_root: str | Path | None = None,
) -> str:
    validate_hierarchy_context_pack(
        context_pack,
        report=report,
        document=document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        project_state=project_state,
        package_root=package_root,
    )
    prompt = _render_hierarchy_prompt_unchecked(context_pack)
    if len(prompt) > context_pack["context_budget"]["max_prompt_chars"]:
        raise DraftStructureError("hierarchy prompt exceeds its sealed budget")
    return prompt


def validate_hierarchy_response(
    response: dict[str, Any],
    context_pack: dict[str, Any],
    *,
    report: dict[str, Any],
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    package_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    validate_hierarchy_context_pack(
        context_pack,
        report=report,
        document=document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        project_state=project_state,
        package_root=package_root,
    )
    if not isinstance(response, dict):
        raise DraftStructureError("hierarchy response must be an object")
    _require_exact_fields(
        response,
        {
            "schema_version",
            "report_sha256",
            "skeleton_sha256",
            "context_pack_sha256",
            "actions",
            "abstentions",
        },
        owner="hierarchy response",
    )
    if response.get("schema_version") != HIERARCHY_RESPONSE_VERSION:
        raise DraftStructureError("hierarchy response version differs")
    if response.get("report_sha256") != context_pack["report_sha256"]:
        raise DraftStructureError("hierarchy response report identity differs")
    if response.get("skeleton_sha256") != context_pack["skeleton_sha256"]:
        raise DraftStructureError("hierarchy response skeleton identity differs")
    if response.get("context_pack_sha256") != context_pack["integrity"][
        "payload_sha256"
    ]:
        raise DraftStructureError("hierarchy response context identity differs")
    actions = response.get("actions")
    abstentions = response.get("abstentions")
    if not isinstance(actions, list) or not isinstance(abstentions, list):
        raise DraftStructureError("hierarchy actions and abstentions must be lists")
    allowed = [str(item) for item in context_pack["allowed_unit_ids"]]
    allowed_set = set(allowed)
    order = {unit_id: index for index, unit_id in enumerate(allowed)}
    covered: list[str] = []
    prepared: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise DraftStructureError(
                f"hierarchy response.actions[{index}] must be an object"
            )
        action_type = action.get("action_type")
        if action_type == "set_parent":
            expected_fields = {
                "action_type",
                "child_unit_id",
                "parent_unit_id",
            }
        elif action_type == "clear_parent":
            expected_fields = {"action_type", "child_unit_id"}
        else:
            raise DraftStructureError("hierarchy response action type differs")
        _require_exact_fields(
            action,
            expected_fields,
            owner=f"hierarchy response.actions[{index}]",
        )
        child_id = str(action.get("child_unit_id") or "")
        if child_id not in allowed_set:
            raise DraftStructureError("hierarchy response references unknown child")
        if action_type == "set_parent":
            parent_id = str(action.get("parent_unit_id") or "")
            if parent_id not in allowed_set:
                raise DraftStructureError(
                    "hierarchy response references unknown parent"
                )
            if order[parent_id] >= order[child_id]:
                raise DraftStructureError(
                    "hierarchy response parent must occur before child"
                )
        covered.append(child_id)
        prepared.append(copy.deepcopy(action))
    for index, abstention in enumerate(abstentions):
        if not isinstance(abstention, dict):
            raise DraftStructureError(
                f"hierarchy response.abstentions[{index}] must be an object"
            )
        _require_exact_fields(
            abstention,
            {"child_unit_id", "reason"},
            owner=f"hierarchy response.abstentions[{index}]",
        )
        child_id = str(abstention.get("child_unit_id") or "")
        if child_id not in allowed_set:
            raise DraftStructureError(
                "hierarchy response abstention references unknown child"
            )
        if abstention.get("reason") not in ABSTENTION_REASONS:
            raise DraftStructureError("hierarchy abstention reason differs")
        covered.append(child_id)
    if (
        sorted(covered, key=order.__getitem__) != allowed
        or len(covered) != len(set(covered))
    ):
        raise DraftStructureError(
            "hierarchy response must exact-cover units in source order"
        )
    return prepared


def run_hierarchy_assistant(
    executor: StructureAssistantExecutor,
    report: dict[str, Any],
    document: dict[str, Any],
    *,
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    admitted_projection: dict[str, Any],
    project_state: dict[str, Any],
    model_identifier: str,
    package_root: str | Path | None = None,
    budget: StructureContextBudget | None = None,
) -> dict[str, Any]:
    if not isinstance(model_identifier, str) or not model_identifier.strip():
        raise DraftStructureError("model_identifier must be a non-empty string")
    context_pack = build_hierarchy_context_pack(
        report,
        document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        project_state=project_state,
        package_root=package_root,
        budget=budget,
    )
    prompt = render_hierarchy_prompt(
        context_pack,
        report=report,
        document=document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        project_state=project_state,
        package_root=package_root,
    )
    pack_copy = copy.deepcopy(context_pack)
    response = executor.complete(prompt, context_pack=pack_copy)
    if pack_copy != context_pack:
        raise DraftStructureError("hierarchy assistant mutated its context pack")
    action_specs = validate_hierarchy_response(
        response,
        context_pack,
        report=report,
        document=document,
        structure_manifest=structure_manifest,
        asset_manifest=asset_manifest,
        admitted_projection=admitted_projection,
        project_state=project_state,
        package_root=package_root,
    )
    plan = build_hierarchy_plan(
        report,
        action_specs,
        proposer={"kind": "llm", "identifier": model_identifier.strip()},
    )
    return {
        "context_pack": context_pack,
        "response": copy.deepcopy(response),
        "hierarchy_plan": plan,
    }


def build_boundary_expansion(
    document: dict[str, Any],
    context_pack: dict[str, Any],
    boundary_id: str,
    *,
    left_blocks: int,
    right_blocks: int,
    budget: StructureContextBudget | None = None,
) -> dict[str, Any]:
    active_budget = budget or StructureContextBudget()
    active_budget.validate()
    if not 0 < left_blocks <= active_budget.max_expansion_blocks_per_side:
        raise DraftStructureError("left_blocks exceeds expansion limit")
    if not 0 < right_blocks <= active_budget.max_expansion_blocks_per_side:
        raise DraftStructureError("right_blocks exceeds expansion limit")
    _validate_seal(context_pack, owner="context pack")
    if canonical_json_sha256(document) != context_pack.get("document_sha256"):
        raise DraftStructureError("expansion document differs from context pack")
    cases = context_pack.get("boundary_cases")
    if not isinstance(cases, list):
        raise DraftStructureError("context pack boundary_cases must be a list")
    matches = [row for row in cases if row.get("boundary_id") == boundary_id]
    if len(matches) != 1:
        raise DraftStructureError("boundary_id is not uniquely exposed by context pack")
    blocks, _block_to_chapter = _flatten_document(document)
    order = {str(block["block_id"]): index for index, block in enumerate(blocks)}
    at_block_id = str(matches[0]["at_block_id"])
    pivot = order.get(at_block_id)
    if pivot is None:
        raise DraftStructureError("boundary block is absent from document")
    start = max(0, pivot - left_blocks)
    stop = min(len(blocks), pivot + 1 + right_blocks)
    payload = {
        "schema_version": EXPANSION_VERSION,
        "doc_id": context_pack["doc_id"],
        "document_sha256": context_pack["document_sha256"],
        "context_pack_sha256": context_pack["integrity"]["payload_sha256"],
        "boundary_id": boundary_id,
        "at_block_id": at_block_id,
        "left_blocks": [
            _preview_text(block, limit=active_budget.expansion_preview_chars)
            for block in blocks[start:pivot]
        ],
        "pivot": _preview_text(
            blocks[pivot],
            limit=active_budget.expansion_preview_chars,
        ),
        "right_blocks": [
            _preview_text(block, limit=active_budget.expansion_preview_chars)
            for block in blocks[pivot + 1 : stop]
        ],
    }
    return _seal_payload(payload)


def _require_exact_fields(
    value: dict[str, Any],
    fields: set[str],
    *,
    owner: str,
) -> None:
    actual = set(value)
    if actual != fields:
        raise DraftStructureError(
            f"{owner} fields differ; missing={sorted(fields - actual)}, "
            f"extra={sorted(actual - fields)}"
        )


def _action_target_units(action: dict[str, Any]) -> set[str]:
    if action["action_type"] in {"update_unit", "split_unit"}:
        return {str(action["unit_id"])}
    return {str(action["left_unit_id"]), str(action["right_unit_id"])}


def _validate_action_scope(action: dict[str, Any], pack: dict[str, Any]) -> None:
    action_type = action.get("action_type")
    if action_type not in _ACTION_FIELDS:
        raise DraftStructureError("LLM response contains unsupported action_type")
    _require_exact_fields(
        action,
        _ACTION_FIELDS[str(action_type)],
        owner="LLM response action",
    )
    scope = pack["allowed_scope"]
    if action_type == "update_unit":
        if action["unit_id"] not in scope["update_unit_ids"]:
            raise DraftStructureError("LLM update_unit is outside exposed scope")
    elif action_type == "split_unit":
        key = {
            "unit_id": action["unit_id"],
            "at_block_id": action["at_block_id"],
        }
        if key not in scope["split_boundaries"]:
            raise DraftStructureError("LLM split_unit is outside exposed scope")
    else:
        key = {
            "left_unit_id": action["left_unit_id"],
            "right_unit_id": action["right_unit_id"],
        }
        if key not in scope["merge_boundaries"]:
            raise DraftStructureError(
                "LLM merge_adjacent_units is outside exposed scope"
            )


def validate_structure_response(
    response: dict[str, Any],
    report: dict[str, Any],
    context_pack: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        raise DraftStructureError("LLM response must be an object")
    _require_exact_fields(
        response,
        {
            "schema_version",
            "report_sha256",
            "context_pack_sha256",
            "actions",
            "abstentions",
        },
        owner="LLM response",
    )
    _validate_seal(context_pack, owner="context pack")
    if response["schema_version"] != RESPONSE_VERSION:
        raise DraftStructureError("LLM response schema_version differs")
    if response["report_sha256"] != report["integrity"]["payload_sha256"]:
        raise DraftStructureError("LLM response report identity differs")
    if response["context_pack_sha256"] != context_pack["integrity"]["payload_sha256"]:
        raise DraftStructureError("LLM response context pack identity differs")
    actions = response["actions"]
    abstentions = response["abstentions"]
    if not isinstance(actions, list) or not isinstance(abstentions, list):
        raise DraftStructureError("LLM actions and abstentions must be lists")
    covered: list[str] = []
    prepared: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            raise DraftStructureError("LLM response action must be an object")
        _validate_action_scope(action, context_pack)
        covered.extend(sorted(_action_target_units(action)))
        prepared.append(copy.deepcopy(action))
    for abstention in abstentions:
        if not isinstance(abstention, dict):
            raise DraftStructureError("LLM abstention must be an object")
        _require_exact_fields(
            abstention,
            {"unit_id", "reason"},
            owner="LLM abstention",
        )
        unit_id = str(abstention["unit_id"])
        if unit_id not in context_pack["focus_unit_ids"]:
            raise DraftStructureError("LLM abstention is outside exposed scope")
        if abstention["reason"] not in ABSTENTION_REASONS:
            raise DraftStructureError("LLM abstention reason is unsupported")
        covered.append(unit_id)
    expected = sorted(str(item) for item in context_pack["focus_unit_ids"])
    if sorted(covered) != expected:
        raise DraftStructureError(
            "LLM response must cover every focus unit exactly once"
        )
    return prepared


def run_structure_assistant(
    executor: StructureAssistantExecutor,
    report: dict[str, Any],
    document: dict[str, Any],
    *,
    model_identifier: str,
    budget: StructureContextBudget | None = None,
    include_all_units: bool = False,
    focus_unit_ids: list[str] | None = None,
    response_contract_dialect: str = BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
) -> dict[str, Any]:
    if not isinstance(model_identifier, str) or not model_identifier.strip():
        raise DraftStructureError("model_identifier must be a non-empty string")
    packs = build_structure_context_packs(
        report,
        document,
        budget=budget,
        include_all_units=include_all_units,
        focus_unit_ids=focus_unit_ids,
        response_contract_dialect=response_contract_dialect,
    )
    responses: list[dict[str, Any]] = []
    action_specs: list[dict[str, Any]] = []
    for pack in packs:
        prompt = render_structure_prompt(
            pack,
            response_contract_dialect=response_contract_dialect,
        )
        pack_copy = copy.deepcopy(pack)
        response = executor.complete(prompt, context_pack=pack_copy)
        if pack_copy != pack:
            raise DraftStructureError("structure assistant mutated its context pack")
        action_specs.extend(
            validate_structure_response(response, report, pack)
        )
        responses.append(copy.deepcopy(response))
    plan = build_correction_plan(
        report,
        action_specs,
        proposer={"kind": "llm", "identifier": model_identifier.strip()},
    )
    return {
        "context_packs": packs,
        "responses": responses,
        "correction_plan": plan,
    }


__all__ = [
    "ABSTENTION_REASONS",
    "BOUNDARY_REPAIR_SCHEMA_DIALECT_V1",
    "BOUNDARY_REPAIR_SCHEMA_DIALECT_V2",
    "BOUNDARY_REPAIR_PROMPT_ID",
    "BOUNDARY_REPAIR_RESPONSE_SCHEMA_ID",
    "BOUNDARY_REPAIR_SEMANTIC_EXTENSION_ID",
    "BOUNDARY_REPAIR_VALIDATOR_ID",
    "CONTEXT_PACK_VERSION",
    "CONTEXT_POLICY_VERSION",
    "EXPANSION_VERSION",
    "GLOBAL_CONTEXT_PACK_VERSION",
    "GLOBAL_RESPONSE_VERSION",
    "HIERARCHY_CONTEXT_PACK_VERSION",
    "HIERARCHY_RESPONSE_VERSION",
    "RESPONSE_VERSION",
    "StructureAssistantExecutor",
    "StructureContextBudget",
    "boundary_repair_contract_identities",
    "build_boundary_expansion",
    "build_global_structure_context_packs",
    "build_hierarchy_context_pack",
    "build_structure_context_packs",
    "parse_structure_response_json",
    "render_global_structure_prompt",
    "render_hierarchy_prompt",
    "render_structure_prompt",
    "run_global_structure_assistant",
    "run_hierarchy_assistant",
    "run_structure_assistant",
    "validate_global_structure_context_pack",
    "validate_global_structure_response",
    "validate_hierarchy_context_pack",
    "validate_hierarchy_response",
    "validate_structure_response",
]
