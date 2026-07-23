"""Full-restored-text semantic contract for the D2L quality Auditor."""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Callable, Mapping, Sequence

from pipeline.llm_backend import canonical_json, canonical_sha256
from pipeline.translate.d2l_translation_quality_auditor_v1 import (
    AuditContractError,
    AuditPacketCaps,
    ISSUE_TYPES,
    SEVERITIES,
)


INPUT_CONTRACT_VERSION = "d2l_translation_quality_full_text_input_v2"
RESPONSE_CONTRACT_VERSION = "d2l_translation_quality_audit_response_v2"
PROMPT_ID = "d2l_translation_quality_audit_full_text_v2_1"
RESPONSE_SCHEMA_ID = "d2l_translation_quality_audit_full_text_response_schema_v2"
LOCAL_VALIDATOR_ID = "d2l_translation_quality_audit_full_text_validator_v2_1"
SEMANTIC_CONTRACT_VERSION = "d2l_translation_quality_full_text_semantic_contract_v2_1"
INTEGRITY_POLICY_ID = "d2l_translation_quality_full_text_integrity_v1"

_PACKET_FIELDS = {
    "contract_version",
    "window_id",
    "source_language",
    "target_language",
    "blocks",
    "glossary_cards",
    "integrity_receipt",
}
_BLOCK_FIELDS = {
    "block_id",
    "block_type",
    "source_full_text",
    "target_full_text",
    "applicable_glossary_refs",
}
_GLOSSARY_FIELDS = {
    "glossary_ref",
    "source_term",
    "allowed_target_variants",
    "policy",
}
_RECEIPT_FIELDS = {
    "policy_id",
    "full_text_restored",
    "source_target_math_byte_exact",
    "source_target_structure_order_equal",
    "forbidden_control_characters_absent",
    "protected_content_read_only",
}
_RESPONSE_FIELDS = {
    "contract_version",
    "window_id",
    "audited_block_ids",
    "findings",
}
_FINDING_FIELDS = {
    "block_id",
    "issue_type",
    "severity",
    "source_evidence",
    "target_evidence",
    "reason",
}
_GLOSSARY_POLICIES = {
    "mandatory", "preserve", "context_sensitive", "advisory"
}
_PROTECTED_REF_RE = re.compile(
    r"\[\[(?:MATH_REF|STRUCT_REF|FORMAT_REF|LINE_REF)_[^\]\r\n]*\]\]"
)
_FORBIDDEN_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


SYSTEM_PROMPT = """You are the Technical Translation Quality Auditor for English-to-Vietnamese
technical book translation.

You receive the final canonical source text and the final Vietnamese candidate
after code has restored all protected mathematics, markup, directives, and line
structure. Judge the same complete text that downstream publication will use.

AUTHORITY
- Report concrete semantic findings only. Do not rewrite the translation.
- Do not decide PASS, RETRY, HOLD, publication, or score.
- Use only the supplied source, target, glossary, and integrity receipt.
- Do not compare against an imagined reference translation.

INTEGRITY BOUNDARY
- LaTeX, inline code, directives, markup, and line structure are visible only to
  provide semantic context. They are read-only.
- The integrity receipt is authoritative: code already proved exact mathematics,
  protected structure order, restoration, and control-character safety.
- Do not report a defect merely because LaTeX or markup is present, formatted
  differently for display, or looks awkward as source text.
- You may use a visible formula to understand surrounding prose. Report only a
  concrete mismatch in the translated prose or its stated relationship to that
  formula.

REPORT ONLY WHEN DIRECT EVIDENCE SHOWS
1. meaningful source content is omitted;
2. a factual or logical claim is added;
3. polarity, negation, modality, or certainty changes materially;
4. a number, comparison, ordering, or prose description of a relation is wrong;
5. an actor, referent, scope, dependency, or cause changes;
6. a technical term has the wrong meaning in this context;
7. meaningful English content remains untranslated;
8. the target is locally incoherent.

Do not treat merely awkward but accurate Vietnamese as a major defect. Use an
advisory only for a concrete fluency problem. When evidence is insufficient,
omit the finding.

EVIDENCE
- source_evidence and target_evidence must be exact substrings of their block.
- For meaning_omission only, target_evidence may be empty.
- For unsupported_addition only, source_evidence may be empty.
- Prefer the shortest lexical evidence that proves the issue. Do not quote a
  whole formula when the issue is in adjacent prose.

CLOSED LABELS
- issue_type must be exactly one of:
  meaning_omission, unsupported_addition, polarity_or_negation_error,
  numeric_or_comparison_error, relation_or_logic_error,
  referent_or_scope_error, terminology_context_error,
  untranslated_source_content, local_coherence_error,
  style_or_fluency_advisory, semantic_other.
- severity must be exactly major or advisory. Never emit minor.
- style_or_fluency_advisory must use advisory severity.

Return JSON only with exactly this shape:
{
  "contract_version": "d2l_translation_quality_audit_response_v2",
  "window_id": "<exact packet window_id>",
  "audited_block_ids": ["<every packet block_id in exact order>"],
  "findings": [
    {
      "block_id": "<packet block_id>",
      "issue_type": "meaning_omission|unsupported_addition|polarity_or_negation_error|numeric_or_comparison_error|relation_or_logic_error|referent_or_scope_error|terminology_context_error|untranslated_source_content|local_coherence_error|style_or_fluency_advisory|semantic_other",
      "severity": "major|advisory",
      "source_evidence": "<exact substring or allowed empty>",
      "target_evidence": "<exact substring or allowed empty>",
      "reason": "<brief evidence-grounded explanation without replacement text>"
    }
  ]
}
"""

USER_TEMPLATE = """Audit every block in this packet. The full restored source and target are
model-visible for semantic understanding; protected content remains read-only.

PACKET:
{{packet_json}}"""

REASK_TEMPLATE = """Your previous response failed the local JSON/contract validator.
Return the complete JSON object again. Do not discuss the errors and do not add
fields. Validator errors:
{{errors_json}}"""


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contract_version", "window_id", "audited_block_ids", "findings"],
    "properties": {
        "contract_version": {"type": "string", "const": RESPONSE_CONTRACT_VERSION},
        "window_id": {"type": "string", "minLength": 1},
        "audited_block_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(_FINDING_FIELDS),
                "properties": {
                    "block_id": {"type": "string", "minLength": 1},
                    "issue_type": {"type": "string", "enum": sorted(ISSUE_TYPES)},
                    "severity": {"type": "string", "enum": sorted(SEVERITIES)},
                    "source_evidence": {"type": "string"},
                    "target_evidence": {"type": "string"},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 800},
                },
            },
        },
    },
}


def build_packet(
    *,
    window_id: str,
    blocks: Sequence[Mapping[str, Any]],
    glossary_cards: Sequence[Mapping[str, Any]],
    integrity_receipt: Mapping[str, Any],
    caps: AuditPacketCaps,
    glossary_token_counter: Callable[[str], int],
) -> dict[str, Any]:
    caps.validate()
    canonical_blocks = [_canonical_block(row) for row in blocks]
    if not window_id or not canonical_blocks:
        raise AuditContractError("V2 packet requires a window and blocks")
    block_ids = [row["block_id"] for row in canonical_blocks]
    if len(set(block_ids)) != len(block_ids):
        raise AuditContractError("V2 packet block IDs must be unique")
    cards = [_canonical_card(row) for row in glossary_cards]
    refs = [row["glossary_ref"] for row in cards]
    if len(set(refs)) != len(refs):
        raise AuditContractError("V2 glossary refs must be unique")
    if len(cards) > caps.max_glossary_cards_per_window:
        raise AuditContractError("V2 glossary card cap exceeded")
    if glossary_token_counter(canonical_json(cards)) > caps.max_glossary_tokens_per_window:
        raise AuditContractError("V2 glossary token cap exceeded")
    known_refs = set(refs)
    for row in canonical_blocks:
        unknown = set(row["applicable_glossary_refs"]) - known_refs
        if unknown:
            raise AuditContractError(f"V2 block references unknown glossary refs: {sorted(unknown)}")
    receipt = _canonical_receipt(integrity_receipt)
    return {
        "contract_version": INPUT_CONTRACT_VERSION,
        "window_id": window_id,
        "source_language": "en",
        "target_language": "vi",
        "blocks": canonical_blocks,
        "glossary_cards": cards,
        "integrity_receipt": receipt,
    }


def validate_packet(
    packet: Mapping[str, Any],
    *, caps: AuditPacketCaps,
    glossary_token_counter: Callable[[str], int],
) -> dict[str, Any]:
    _require_fields(packet, _PACKET_FIELDS, "V2 packet")
    expected = build_packet(
        window_id=str(packet.get("window_id") or ""),
        blocks=packet.get("blocks") if isinstance(packet.get("blocks"), list) else [],
        glossary_cards=(
            packet.get("glossary_cards")
            if isinstance(packet.get("glossary_cards"), list)
            else []
        ),
        integrity_receipt=(
            packet.get("integrity_receipt")
            if isinstance(packet.get("integrity_receipt"), Mapping)
            else {}
        ),
        caps=caps,
        glossary_token_counter=glossary_token_counter,
    )
    if dict(packet) != expected:
        raise AuditContractError("V2 packet is not canonical")
    return expected


def render_messages(
    packet: Mapping[str, Any],
    *, caps: AuditPacketCaps,
    glossary_token_counter: Callable[[str], int],
) -> list[dict[str, str]]:
    canonical = validate_packet(
        packet, caps=caps, glossary_token_counter=glossary_token_counter
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_TEMPLATE.replace("{{packet_json}}", canonical_json(canonical)),
        },
    ]


def reask_note(errors: Sequence[str]) -> str:
    return REASK_TEMPLATE.replace(
        "{{errors_json}}", canonical_json([str(value) for value in errors[:12]])
    )


def parse_response(text: str) -> Mapping[str, Any]:
    duplicates: list[str] = []

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                duplicates.append(str(key))
            result[str(key)] = value
        return result

    try:
        value = json.loads(str(text), object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise AuditContractError(f"V2 audit JSON parse failed: {exc}") from exc
    if duplicates:
        raise AuditContractError(f"V2 audit JSON duplicates keys: {duplicates}")
    if not isinstance(value, Mapping):
        raise AuditContractError("V2 audit response must be an object")
    return value


def validate_response(response: Mapping[str, Any], packet: Mapping[str, Any]) -> dict[str, Any]:
    _require_fields(response, _RESPONSE_FIELDS, "V2 response")
    if response.get("contract_version") != RESPONSE_CONTRACT_VERSION:
        raise AuditContractError("V2 response contract version mismatch")
    if response.get("window_id") != packet.get("window_id"):
        raise AuditContractError("V2 response window mismatch")
    expected_ids = [str(row["block_id"]) for row in packet["blocks"]]
    if response.get("audited_block_ids") != expected_ids:
        raise AuditContractError("V2 response does not exact-cover block IDs")
    raw_findings = response.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > 64:
        raise AuditContractError("V2 findings must be a bounded array")
    blocks = {str(row["block_id"]): row for row in packet["blocks"]}
    findings: list[dict[str, str]] = []
    signatures: set[tuple[str, ...]] = set()
    for index, raw in enumerate(raw_findings):
        if not isinstance(raw, Mapping):
            raise AuditContractError(f"V2 finding {index} must be an object")
        _require_fields(raw, _FINDING_FIELDS, f"V2 finding {index}")
        if any(not isinstance(raw.get(key), str) for key in _FINDING_FIELDS):
            raise AuditContractError(f"V2 finding {index} fields must be strings")
        block_id = str(raw["block_id"])
        issue_type = str(raw["issue_type"])
        severity = str(raw["severity"])
        source_evidence = str(raw["source_evidence"])
        target_evidence = str(raw["target_evidence"])
        reason = str(raw["reason"])
        if block_id not in blocks:
            raise AuditContractError(
                f"V2 finding {index} has foreign block_id {block_id!r}"
            )
        if issue_type not in ISSUE_TYPES:
            raise AuditContractError(
                f"V2 finding {index} has unknown issue_type {issue_type!r}; "
                f"allowed={sorted(ISSUE_TYPES)}"
            )
        if severity not in SEVERITIES:
            raise AuditContractError(
                f"V2 finding {index} has unknown severity {severity!r}; "
                f"allowed={sorted(SEVERITIES)}"
            )
        if issue_type == "style_or_fluency_advisory" and severity != "advisory":
            raise AuditContractError(
                f"V2 finding {index} style advisory requires advisory severity"
            )
        if not reason or len(reason) > 800:
            raise AuditContractError(f"V2 finding {index} reason is invalid")
        if source_evidence:
            if source_evidence not in blocks[block_id]["source_full_text"]:
                raise AuditContractError(f"V2 finding {index} source evidence is not exact")
        elif issue_type != "unsupported_addition":
            raise AuditContractError(f"V2 finding {index} requires source evidence")
        if target_evidence:
            if target_evidence not in blocks[block_id]["target_full_text"]:
                raise AuditContractError(f"V2 finding {index} target evidence is not exact")
        elif issue_type != "meaning_omission":
            raise AuditContractError(f"V2 finding {index} requires target evidence")
        row = {
            "block_id": block_id,
            "issue_type": issue_type,
            "severity": severity,
            "source_evidence": source_evidence,
            "target_evidence": target_evidence,
            "reason": reason,
        }
        signature = tuple(row[key] for key in sorted(row))
        if signature in signatures:
            raise AuditContractError(f"V2 finding {index} is duplicated")
        signatures.add(signature)
        findings.append(row)
    return {
        "contract_version": RESPONSE_CONTRACT_VERSION,
        "window_id": str(response["window_id"]),
        "audited_block_ids": expected_ids,
        "findings": findings,
    }


def build_semantic_manifest(
    *, deterministic_policy_id: str, deterministic_policy_sha256: str,
    state_policy_id: str, caps: AuditPacketCaps, glossary_token_counter_id: str,
) -> dict[str, Any]:
    body = {
        "schema_version": SEMANTIC_CONTRACT_VERSION,
        "prompt_id": PROMPT_ID,
        "prompt_sha256": prompt_sha256(),
        "input_contract_version": INPUT_CONTRACT_VERSION,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "response_schema_id": RESPONSE_SCHEMA_ID,
        "response_schema_sha256": response_schema_sha256(),
        "local_validator_id": LOCAL_VALIDATOR_ID,
        "integrity_policy_id": INTEGRITY_POLICY_ID,
        "deterministic_policy_id": deterministic_policy_id,
        "deterministic_policy_sha256": deterministic_policy_sha256,
        "state_policy_id": state_policy_id,
        "glossary_token_counter_id": glossary_token_counter_id,
        "packet_caps": {
            "max_glossary_cards_per_window": caps.max_glossary_cards_per_window,
            "max_glossary_tokens_per_window": caps.max_glossary_tokens_per_window,
        },
    }
    return {**body, "manifest_sha256": canonical_sha256(body).upper()}


def prompt_sha256() -> str:
    return sha256((SYSTEM_PROMPT + "\n" + USER_TEMPLATE + "\n" + REASK_TEMPLATE).encode("utf-8")).hexdigest().upper()


def response_schema_sha256() -> str:
    return canonical_sha256(RESPONSE_SCHEMA).upper()


def _canonical_block(raw: Mapping[str, Any]) -> dict[str, Any]:
    _require_fields(raw, _BLOCK_FIELDS, "V2 block")
    block_id = str(raw.get("block_id") or "")
    block_type = str(raw.get("block_type") or "")
    source = raw.get("source_full_text")
    target = raw.get("target_full_text")
    refs = raw.get("applicable_glossary_refs")
    if (
        not block_id
        or not block_type
        or not isinstance(source, str)
        or not source
        or not isinstance(target, str)
        or not target
    ):
        raise AuditContractError("V2 block is incomplete")
    if _PROTECTED_REF_RE.search(source) or _PROTECTED_REF_RE.search(target):
        raise AuditContractError("V2 full text still contains a protected reference")
    if _FORBIDDEN_CONTROL_RE.search(source) or _FORBIDDEN_CONTROL_RE.search(target):
        raise AuditContractError("V2 full text contains a forbidden control character")
    if not isinstance(refs, list) or any(not isinstance(value, str) for value in refs):
        raise AuditContractError("V2 block glossary refs must be strings")
    if len(set(refs)) != len(refs):
        raise AuditContractError("V2 block glossary refs must be unique")
    return {
        "block_id": block_id,
        "block_type": block_type,
        "source_full_text": source,
        "target_full_text": target,
        "applicable_glossary_refs": list(refs),
    }


def _canonical_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    _require_fields(raw, _GLOSSARY_FIELDS, "V2 glossary card")
    variants = raw.get("allowed_target_variants")
    if not isinstance(variants, list) or not variants or any(not isinstance(value, str) or not value for value in variants):
        raise AuditContractError("V2 glossary variants must be nonempty strings")
    policy = str(raw.get("policy") or "")
    if policy not in _GLOSSARY_POLICIES:
        raise AuditContractError("V2 glossary policy is invalid")
    return {
        "glossary_ref": str(raw.get("glossary_ref") or ""),
        "source_term": str(raw.get("source_term") or ""),
        "allowed_target_variants": list(variants),
        "policy": policy,
    }


def _canonical_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    _require_fields(raw, _RECEIPT_FIELDS, "V2 integrity receipt")
    if raw.get("policy_id") != INTEGRITY_POLICY_ID:
        raise AuditContractError("V2 integrity policy mismatch")
    for key in _RECEIPT_FIELDS - {"policy_id"}:
        if raw.get(key) is not True:
            raise AuditContractError(f"V2 integrity receipt requires {key}=true")
    return {key: raw[key] for key in sorted(_RECEIPT_FIELDS)}


def _require_fields(raw: Mapping[str, Any], expected: set[str], owner: str) -> None:
    observed = set(raw)
    if observed != expected:
        raise AuditContractError(
            f"{owner} fields mismatch; missing={sorted(expected-observed)} extra={sorted(observed-expected)}"
        )


__all__ = [
    "INPUT_CONTRACT_VERSION", "INTEGRITY_POLICY_ID", "LOCAL_VALIDATOR_ID",
    "PROMPT_ID", "RESPONSE_CONTRACT_VERSION", "RESPONSE_SCHEMA",
    "RESPONSE_SCHEMA_ID", "AuditContractError", "build_packet",
    "build_semantic_manifest", "parse_response", "prompt_sha256", "reask_note",
    "render_messages", "response_schema_sha256", "validate_packet",
    "validate_response",
]
