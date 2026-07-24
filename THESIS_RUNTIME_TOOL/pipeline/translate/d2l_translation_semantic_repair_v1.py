"""Versioned, source-grounded semantic repair contract for D2L Translator."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from pipeline.llm_backend import canonical_json, canonical_sha256
from pipeline.translate import d2l_latex_markup_line_protected_spans_v5 as spans_v5
from pipeline.translate.d2l_translation_integrity_v1 import (
    inspect_translations,
    retry_findings,
    warning_findings,
)


INPUT_CONTRACT_VERSION = "d2l_translation_semantic_repair_input_v1"
RESPONSE_CONTRACT_VERSION = "d2l_translation_semantic_repair_response_v1"
PROMPT_ID = "d2l_translation_semantic_repair_v1_1_bracketed_fixed_only"
LOCAL_VALIDATOR_ID = "d2l_translation_semantic_repair_validator_v1_0"

_FINDING_FIELDS = {
    "block_id",
    "issue_type",
    "severity",
    "source_evidence",
    "target_evidence",
    "reason",
}
_TRANSLATOR_CONTEXT_FIELDS = {
    "context_pack_sha256",
    "glossary_lines",
    "preserve_lines",
    "context_sensitive_lines",
    "entity_lines",
    "address_lines",
}


SYSTEM_PROMPT = """You are repairing an English-to-Vietnamese technical
translation produced by the same Translator role.

AUTHORITY
- The complete protected English active window is the source of truth.
- The current Vietnamese translation is the editing base, not a new draft.
- The supplied Translator context has the same terminology and preservation
  authority as the initial S1 translation. Auditor findings never override it.
- Active semantic findings identify meaning defects. They never prescribe a
  replacement term or authorize unrelated rewriting.
- Resolved integrity history names defects that must not reappear.

SCOPE
- Read every context block for coherence.
- Edit only output_block_ids and return every one exactly once in that order.
- Preserve all accurate content and wording outside the stated defect.
- Copy every opaque MATH_REF, STRUCT_REF, FORMAT_REF, and LINE_REF in each
  editable current target exactly once and in the same order.
- Do not add explanations, alternatives, terminology advice, or metadata.

Return JSON only using the required response contract."""

USER_TEMPLATE = """Repair only the authorized blocks in this packet.

PACKET:
{{packet_json}}

Return exactly:
{
  "contract_version": "d2l_translation_semantic_repair_response_v1",
  "window_id": "<exact packet window_id>",
  "repairs": [
    {
      "block_id": "<next exact output_block_id>",
      "repaired_target_protected_text": "<complete repaired protected target>"
    }
  ]
}"""


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contract_version", "window_id", "repairs"],
    "properties": {
        "contract_version": {"type": "string", "const": RESPONSE_CONTRACT_VERSION},
        "window_id": {"type": "string", "minLength": 1},
        "repairs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["block_id", "repaired_target_protected_text"],
                "properties": {
                    "block_id": {"type": "string", "minLength": 1},
                    "repaired_target_protected_text": {
                        "type": "string",
                        "minLength": 1,
                    },
                },
            },
        },
    },
}


class SemanticRepairContractError(ValueError):
    """Raised when a repair packet or response violates its closed contract."""


@dataclass(frozen=True)
class SemanticRepairPlan:
    packet: dict[str, Any]
    source_blocks_by_id: dict[str, dict[str, Any]]
    target_plans_by_id: dict[str, spans_v5.ProtectionPlan]


def build_plan(
    *,
    window_id: str,
    arm_id: str,
    source_blocks: Sequence[Mapping[str, Any]],
    current_translations: Mapping[str, Mapping[str, Any]],
    output_block_ids: Sequence[str],
    active_semantic_findings: Sequence[Mapping[str, Any]],
    resolved_integrity_history: Sequence[Mapping[str, Any]],
    original_context_pack: Mapping[str, Any] | None,
) -> SemanticRepairPlan:
    if not window_id or arm_id not in {"s0", "s1"}:
        raise SemanticRepairContractError("Repair window or arm is invalid")
    source_rows = [_canonical_source_block(row) for row in source_blocks]
    source_by_id = {row["block_id"]: row for row in source_rows}
    if len(source_by_id) != len(source_rows) or not source_rows:
        raise SemanticRepairContractError("Repair source IDs must be unique")
    ordered_output_ids = [str(value) for value in output_block_ids]
    if (
        not ordered_output_ids
        or len(set(ordered_output_ids)) != len(ordered_output_ids)
        or any(value not in source_by_id for value in ordered_output_ids)
    ):
        raise SemanticRepairContractError("Repair output IDs are invalid")

    context_rows: list[dict[str, Any]] = []
    target_plans: dict[str, spans_v5.ProtectionPlan] = {}
    for source_row in source_rows:
        block_id = source_row["block_id"]
        source_plan = spans_v5.protect_blocks(
            [
                {
                    "block_id": block_id,
                    "block_type": source_row["block_type"],
                    "clean_text": source_row["source_text"],
                    "source_text": source_row["source_text"],
                }
            ]
        )
        translated = current_translations.get(block_id)
        status = str((translated or {}).get("status") or "missing")
        target = (translated or {}).get("target_text")
        target_protected: str | None = None
        if isinstance(target, str) and target:
            target_plan = spans_v5.protect_blocks(
                [
                    {
                        "block_id": block_id,
                        "block_type": source_row["block_type"],
                        "clean_text": target,
                        "source_text": target,
                    }
                ]
            )
            target_protected = str(target_plan.protected_blocks[0]["clean_text"])
            if block_id in ordered_output_ids:
                target_plans[block_id] = target_plan
        elif block_id in ordered_output_ids:
            raise SemanticRepairContractError(
                f"Editable repair block lacks a current target: {block_id}"
            )
        context_rows.append(
            {
                "block_id": block_id,
                "block_type": source_row["block_type"],
                "source_protected_text": str(
                    source_plan.protected_blocks[0]["clean_text"]
                ),
                "current_target_protected_text": target_protected,
                "translation_status": status,
                "editable": block_id in ordered_output_ids,
            }
        )

    findings = _canonical_findings(
        active_semantic_findings,
        source_by_id=source_by_id,
        current_translations=current_translations,
        output_block_ids=set(ordered_output_ids),
    )
    if not findings:
        raise SemanticRepairContractError("Repair requires a major semantic finding")
    packet = {
        "contract_version": INPUT_CONTRACT_VERSION,
        "window_id": window_id,
        "arm_id": arm_id,
        "source_language": "en",
        "target_language": "vi",
        "output_block_ids": ordered_output_ids,
        "context_blocks": context_rows,
        "active_semantic_findings": findings,
        "resolved_integrity_history": _canonical_history(
            resolved_integrity_history,
            source_block_ids=set(source_by_id),
            output_block_ids=set(ordered_output_ids),
        ),
        "translator_context": _canonical_translator_context(
            original_context_pack,
            arm_id=arm_id,
        ),
    }
    return SemanticRepairPlan(
        packet=packet,
        source_blocks_by_id=source_by_id,
        target_plans_by_id=target_plans,
    )


def render_messages(plan: SemanticRepairPlan) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_TEMPLATE.replace(
                "{{packet_json}}", canonical_json(plan.packet)
            ),
        },
    ]


def parse_response(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    duplicates: list[str] = []

    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in rows:
            if key in result:
                duplicates.append(str(key))
            result[str(key)] = item
        return result

    try:
        parsed = json.loads(str(value), object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise SemanticRepairContractError(f"Repair JSON parse failed: {exc}") from exc
    if duplicates or not isinstance(parsed, Mapping):
        raise SemanticRepairContractError("Repair response is not a unique-key object")
    return dict(parsed)


def validate_and_restore(
    response: Mapping[str, Any],
    plan: SemanticRepairPlan,
) -> dict[str, Any]:
    if set(response) != {"contract_version", "window_id", "repairs"}:
        raise SemanticRepairContractError("Repair response fields mismatch")
    packet = plan.packet
    if response.get("contract_version") != RESPONSE_CONTRACT_VERSION:
        raise SemanticRepairContractError("Repair response version mismatch")
    if response.get("window_id") != packet["window_id"]:
        raise SemanticRepairContractError("Repair response window mismatch")
    raw_repairs = response.get("repairs")
    if not isinstance(raw_repairs, list):
        raise SemanticRepairContractError("Repair rows must be an array")
    expected_ids = list(packet["output_block_ids"])
    observed_ids: list[str] = []
    protected_updates: dict[str, str] = {}
    for index, raw in enumerate(raw_repairs):
        if not isinstance(raw, Mapping) or set(raw) != {
            "block_id",
            "repaired_target_protected_text",
        }:
            raise SemanticRepairContractError(f"Repair row {index} fields mismatch")
        block_id = str(raw.get("block_id") or "")
        target = raw.get("repaired_target_protected_text")
        if not isinstance(target, str) or not target:
            raise SemanticRepairContractError(f"Repair row {index} target is empty")
        observed_ids.append(block_id)
        protected_updates[block_id] = target
    if observed_ids != expected_ids or len(protected_updates) != len(expected_ids):
        raise SemanticRepairContractError("Repair rows do not exact-cover output IDs")

    restored_updates: dict[str, str] = {}
    warnings: list[dict[str, Any]] = []
    for block_id in expected_ids:
        target_plan = plan.target_plans_by_id[block_id]
        fixed_only = spans_v5.fixed_only_protected_translations(target_plan)
        protected_target = fixed_only.get(
            block_id,
            protected_updates[block_id],
        )
        restored, issues = spans_v5.restore_translations(
            {block_id: protected_target},
            target_plan,
        )
        if issues or set(restored) != {block_id}:
            codes = sorted({str(issue.issue_type) for issue in issues})
            raise SemanticRepairContractError(
                f"Repair protected-content validation failed for {block_id}: {codes}"
            )
        restored_updates[block_id] = restored[block_id]
        source = plan.source_blocks_by_id[block_id]
        integrity = inspect_translations(
            [
                {
                    "block_id": block_id,
                    "block_type": source["block_type"],
                    "clean_text": source["source_text"],
                    "source_text": source["source_text"],
                }
            ],
            {block_id: restored[block_id]},
        )
        majors = retry_findings(integrity)
        if block_id in fixed_only:
            majors = [
                row
                for row in majors
                if row.issue_type
                not in {"target_equals_source", "untranslated_heading"}
            ]
        if majors:
            codes = sorted({row.issue_type for row in majors})
            raise SemanticRepairContractError(
                f"Repair deterministic validation failed for {block_id}: {codes}"
            )
        warnings.extend(row.to_dict() for row in warning_findings(integrity))
    return {
        "updates": restored_updates,
        "warnings": warnings,
        "output_block_ids": expected_ids,
    }


def prompt_sha256() -> str:
    return canonical_sha256({"system": SYSTEM_PROMPT, "user": USER_TEMPLATE}).upper()


def response_schema_sha256() -> str:
    return canonical_sha256(RESPONSE_SCHEMA).upper()


def _canonical_source_block(raw: Mapping[str, Any]) -> dict[str, Any]:
    block_id = str(raw.get("block_id") or "")
    block_type = str(raw.get("block_type") or "prose")
    source = raw.get("clean_text") or raw.get("source_text")
    if not block_id or not isinstance(source, str) or not source:
        raise SemanticRepairContractError("Repair source block is incomplete")
    return {"block_id": block_id, "block_type": block_type, "source_text": source}


def _canonical_findings(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_by_id: Mapping[str, Mapping[str, Any]],
    current_translations: Mapping[str, Mapping[str, Any]],
    output_block_ids: set[str],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for index, raw in enumerate(rows):
        if set(raw) != _FINDING_FIELDS:
            raise SemanticRepairContractError(f"Semantic finding {index} fields mismatch")
        row = {key: str(raw[key]) for key in _FINDING_FIELDS}
        block_id = row["block_id"]
        if block_id not in output_block_ids or row["severity"] != "major":
            raise SemanticRepairContractError("Repair findings must be major and editable")
        source = str(source_by_id[block_id]["source_text"])
        target = str(current_translations[block_id].get("target_text") or "")
        if row["source_evidence"] and row["source_evidence"] not in source:
            raise SemanticRepairContractError("Repair source evidence is foreign")
        if row["target_evidence"] and row["target_evidence"] not in target:
            raise SemanticRepairContractError("Repair target evidence is foreign")
        if not row["reason"]:
            raise SemanticRepairContractError("Repair finding reason is empty")
        block_type = str(source_by_id[block_id]["block_type"])
        result.append(
            {
                **row,
                "source_evidence": _protect_audit_text(
                    row["source_evidence"],
                    block_type=block_type,
                    block_id=f"finding_{index}_source",
                ),
                "target_evidence": _protect_audit_text(
                    row["target_evidence"],
                    block_type=block_type,
                    block_id=f"finding_{index}_target",
                ),
                "reason": _protect_audit_text(
                    row["reason"],
                    block_type="paragraph",
                    block_id=f"finding_{index}_reason",
                ),
            }
        )
    if {row["block_id"] for row in result} != output_block_ids:
        raise SemanticRepairContractError("Every repair output needs a major finding")
    return result


def _protect_audit_text(value: str, *, block_type: str, block_id: str) -> str:
    if not value:
        return ""
    plan = spans_v5.protect_blocks(
        [
            {
                "block_id": block_id,
                "block_type": block_type,
                "clean_text": value,
                "source_text": value,
            }
        ]
    )
    return str(plan.protected_blocks[0]["clean_text"])


def _canonical_history(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_block_ids: set[str],
    output_block_ids: set[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in rows:
        scope = str(raw.get("scope") or "window")
        block_id = raw.get("block_id")
        block_id = None if block_id is None else str(block_id)
        if scope not in {"block", "window"}:
            raise SemanticRepairContractError("Repair history scope is invalid")
        if block_id is not None and block_id not in source_block_ids:
            raise SemanticRepairContractError("Repair history cites a foreign block")
        if scope == "block" and block_id not in output_block_ids:
            continue
        issue_type = str(raw.get("issue_type") or "mechanical_contract_error")
        evidence = " ".join(str(raw.get("evidence") or "").split())[:240]
        result.append(
            {
                "scope": scope,
                "block_id": block_id,
                "issue_type": issue_type,
                "evidence": evidence,
                "status": "resolved_do_not_regress",
            }
        )
    return result[:24]


def _canonical_translator_context(
    raw: Mapping[str, Any] | None,
    *,
    arm_id: str,
) -> dict[str, Any]:
    keys = _TRANSLATOR_CONTEXT_FIELDS - {"context_pack_sha256"}
    if arm_id == "s0":
        if raw is not None:
            raise SemanticRepairContractError("S0 repair must not receive glossary context")
        return {"context_pack_sha256": None, **{key: [] for key in sorted(keys)}}
    if not isinstance(raw, Mapping):
        raise SemanticRepairContractError("S1 repair requires the original context pack")
    result: dict[str, Any] = {"context_pack_sha256": canonical_sha256(raw).upper()}
    for key in sorted(keys):
        value = raw.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise SemanticRepairContractError(f"S1 context pack field is invalid: {key}")
        result[key] = list(value)
    return result


__all__ = [
    "INPUT_CONTRACT_VERSION",
    "LOCAL_VALIDATOR_ID",
    "PROMPT_ID",
    "RESPONSE_CONTRACT_VERSION",
    "RESPONSE_SCHEMA",
    "SemanticRepairContractError",
    "SemanticRepairPlan",
    "build_plan",
    "parse_response",
    "prompt_sha256",
    "render_messages",
    "response_schema_sha256",
    "validate_and_restore",
]
