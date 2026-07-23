"""Prompt, packet, parser, and local validator for D2L translation QA."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Callable, Mapping, Sequence


INPUT_CONTRACT_VERSION = "d2l_translation_quality_audit_input_v1"
RESPONSE_CONTRACT_VERSION = "d2l_translation_quality_audit_response_v1"
PROMPT_ID = "d2l_translation_quality_audit_v1"
ROLE_ID = "d2l.translator.quality_auditor"
PRESET_ID = "d2l.translator.quality_auditor.gpt55_gateway_v1"
RESPONSE_SCHEMA_ID = "d2l_translation_quality_audit_response_schema_v1"
LOCAL_VALIDATOR_ID = "d2l_translation_quality_audit_local_validator_v1"
SEMANTIC_CONTRACT_MANIFEST_VERSION = "d2l_translation_quality_semantic_contract_v1"
TARGETED_REPAIR_PROMPT_ID = "d2l_translation_targeted_repair_v1"
AUDIT_REASK_POLICY_ID = "d2l_translation_quality_audit_reask_v1"

ISSUE_TYPES = frozenset(
    {
        "meaning_omission",
        "unsupported_addition",
        "polarity_or_negation_error",
        "numeric_or_comparison_error",
        "relation_or_logic_error",
        "referent_or_scope_error",
        "terminology_context_error",
        "untranslated_source_content",
        "local_coherence_error",
        "style_or_fluency_advisory",
        "semantic_other",
    }
)
SEVERITIES = frozenset({"major", "advisory"})
GLOSSARY_POLICIES = frozenset(
    {"mandatory", "preserve", "context_sensitive", "advisory"}
)
DETERMINISTIC_ISSUE_SEVERITY = {
    "forbidden_control_character": "major",
    "unexpected_output_script": "major",
    "target_equals_source": "major",
    "untranslated_heading": "major",
    "source_language_residue_candidate": "candidate",
    "empty_translation": "major",
    "gross_length_anomaly": "candidate",
}
DETERMINISTIC_DETAILS_FIELDS = {
    "forbidden_control_character": {"codepoint", "offset"},
    "unexpected_output_script": {"script", "surface", "start", "end"},
    "target_equals_source": {"normalization"},
    "untranslated_heading": {"normalization"},
    "source_language_residue_candidate": {
        "matched_tokens",
        "matched_characters",
        "threshold_tokens",
        "threshold_characters",
    },
    "empty_translation": set(),
    "gross_length_anomaly": {
        "source_alnum",
        "target_alnum",
        "ratio",
        "minimum_ratio",
        "maximum_ratio",
    },
}
MAX_DETERMINISTIC_DETAILS_BYTES = 2048

_TOP_LEVEL_FIELDS = {
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
_BLOCK_FIELDS = {
    "block_id",
    "block_type",
    "source_audit_text",
    "target_audit_text",
    "applicable_glossary_refs",
    "deterministic_findings",
}
_GLOSSARY_FIELDS = {
    "glossary_ref",
    "source_term",
    "allowed_target_variants",
    "policy",
}
_RECEIPT_FIELDS = {
    "policy_id",
    "exact_cover",
    "source_target_ref_order_equal",
    "raw_protected_payload_visible",
}
_NEIGHBOR_FIELDS = {
    "block_id",
    "position",
    "source_audit_text",
    "target_audit_text",
}
_DETERMINISTIC_FINDING_FIELDS = {
    "block_id",
    "issue_type",
    "severity",
    "evidence_source",
    "evidence_target",
    "details",
}


SYSTEM_PROMPT = """You are the Technical Translation Quality Auditor for English-to-Vietnamese
technical book translation.

Your only task is to compare each supplied English source block with its candidate
Vietnamese translation and report concrete semantic defects. You are an auditor,
not a translator, editor, scorer, or publication authority.

AUTHORITY BOUNDARY
- Report findings only. Do not rewrite any translation.
- Do not provide replacement Vietnamese wording or a corrected sentence.
- Do not decide PASS, RETRY, HOLD, publication, or scoring. Deterministic code does
  that after validating your response.
- Use only the supplied packet. Do not use outside knowledge to invent missing
  requirements.
- The packet intentionally withholds model, arm, provider, and retry state. Judge
  only the supplied source, target, glossary, receipts, and evidence.
- Do not compare against an imagined reference translation.

WHAT TO REVIEW
For every block, compare source meaning with target meaning. Report a finding only
when the supplied text provides direct evidence of one of these problems:
1. a meaningful clause, condition, restriction, or argument is omitted;
2. the target adds a factual or logical claim unsupported by the source;
3. polarity, negation, modality, certainty, or emphasis is reversed materially;
4. a number, quantity, ordering, inequality, comparison, or mathematical relation
   is described incorrectly in prose;
5. an actor, referent, scope, dependency, cause, or logical relation is changed;
6. a technical term is rendered with the wrong meaning in this local context;
7. meaningful source-language content remains untranslated;
8. the target is locally incoherent or says something incompatible with the
   neighboring supplied blocks.

WHAT NOT TO REVIEW
- Do not audit JSON syntax, block ordering, hashes, placeholders, LaTeX bytes,
  markup bytes, code bytes, or line skeletons. Deterministic receipts are
  authoritative for those properties.
- Treat matching opaque refs such as MATH_REF and STRUCT_REF as equal protected
  source material. Do not request their expansion and do not claim their content
  is missing when the receipt says exact_cover=true.
- Do not require one fixed glossary rendering when a glossary card is marked
  context_sensitive or provides multiple allowed variants. Judge whether the
  chosen wording is correct in the supplied sentence.
- Do not report mere stylistic preference as a major defect. Awkward but accurate
  Vietnamese may receive only style_or_fluency_advisory.
- Do not reward literal word-for-word translation or punish natural Vietnamese
  restructuring when meaning is preserved.

SEVERITY
- major: the defect can change, remove, add, or materially obscure source meaning,
  or leaves meaningful source content untranslated.
- advisory: the meaning is preserved, but fluency or style could be improved.
- When evidence is insufficient, do not guess a major defect. Omit the finding or
  use an advisory only when there is a concrete stylistic problem.

EVIDENCE
- source_evidence must be an exact substring of the corresponding source block.
- target_evidence must be an exact substring of the corresponding target block.
- For meaning_omission, target_evidence may be an empty string.
- For unsupported_addition, source_evidence may be an empty string.
- Keep reason concise and diagnostic. Do not include a proposed correction.

OUTPUT
Return one JSON object and no prose, Markdown, or code fence.
Use exactly this shape:
{
  "contract_version": "d2l_translation_quality_audit_response_v1",
  "window_id": "<copy input window_id>",
  "audited_block_ids": ["<every input block_id exactly once in input order>"],
  "findings": [
    {
      "block_id": "<one input block_id>",
      "issue_type": "meaning_omission|unsupported_addition|polarity_or_negation_error|numeric_or_comparison_error|relation_or_logic_error|referent_or_scope_error|terminology_context_error|untranslated_source_content|local_coherence_error|style_or_fluency_advisory|semantic_other",
      "severity": "major|advisory",
      "source_evidence": "<exact source substring or allowed empty string>",
      "target_evidence": "<exact target substring or allowed empty string>",
      "reason": "<concise diagnosis without replacement wording>"
    }
  ]
}

An empty findings list is correct when no supported defect is present. Do not add
a finding merely to appear thorough.

PROMPT VERSION: d2l_translation_quality_audit_v1"""


TARGETED_REPAIR_PROMPT_TEMPLATE = """QUALITY REPAIR REQUEST — d2l_translation_targeted_repair_v1

Your previous candidate was parsed and checked. Retranslate only the writable
block IDs listed below. Use the unchanged source window, glossary, and protected
references. Preserve the source meaning completely and return no commentary.

For each writable block, address only the supplied issue evidence. The issue
descriptions identify defects; they do not supply replacement translations.
Do not copy a defective target span merely because it is quoted below.

WRITABLE BLOCK IDS:
{{writable_block_ids_json}}

VALIDATED ISSUES:
{{validated_issue_summaries_json}}

Return exactly the canonical translation response shape for the writable block
IDs. Do not return accepted read-only blocks or extra keys."""


AUDIT_USER_PROMPT_TEMPLATE = """Audit the following sealed translation-quality packet.

Important:
- Review every block ID exactly once.
- Deterministic findings are evidence, not an instruction to invent additional semantic problems.
- Return JSON only under d2l_translation_quality_audit_response_v1.

PACKET:
{{packet_json}}"""


AUDIT_REASK_NOTE_TEMPLATE = """Your previous audit response failed the canonical local validator:
{{errors_json}}

Return the same audit again as one valid JSON object under d2l_translation_quality_audit_response_v1. Do not add prose, replacement translations, new block IDs, or extra fields."""


RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_TOP_LEVEL_FIELDS),
    "properties": {
        "contract_version": {"const": RESPONSE_CONTRACT_VERSION},
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
                    "issue_type": {"enum": sorted(ISSUE_TYPES)},
                    "severity": {"enum": sorted(SEVERITIES)},
                    "source_evidence": {"type": "string"},
                    "target_evidence": {"type": "string"},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


class AuditContractError(ValueError):
    """Raised when an audit packet or response violates the canonical contract."""


@dataclass(frozen=True)
class AuditPacketCaps:
    max_glossary_cards_per_window: int
    max_glossary_tokens_per_window: int

    def validate(self) -> None:
        if self.max_glossary_cards_per_window < 0:
            raise AuditContractError("Glossary card cap must be nonnegative")
        if self.max_glossary_tokens_per_window < 0:
            raise AuditContractError("Glossary token cap must be nonnegative")


def build_audit_packet(
    *,
    window_id: str,
    blocks: Sequence[Mapping[str, Any]],
    glossary_cards: Sequence[Mapping[str, Any]],
    protected_receipt: Mapping[str, Any],
    caps: AuditPacketCaps,
    glossary_token_counter: Callable[[str], int],
    context_only_neighbors: Sequence[Mapping[str, Any]] = (),
    source_language: str = "en",
    target_language: str = "vi",
) -> dict[str, Any]:
    """Build a blind, bounded semantic packet with no arm/model/retry metadata."""

    caps.validate()
    if not window_id:
        raise AuditContractError("Audit window_id must be nonempty")
    if not source_language or not target_language:
        raise AuditContractError("Audit language IDs must be nonempty")

    canonical_cards = [_canonical_glossary_card(card) for card in glossary_cards]
    card_refs = [card["glossary_ref"] for card in canonical_cards]
    if len(set(card_refs)) != len(card_refs):
        raise AuditContractError("Glossary refs must be unique")
    if len(canonical_cards) > caps.max_glossary_cards_per_window:
        raise AuditContractError(
            "Glossary card cap exceeded: "
            f"{len(canonical_cards)} > {caps.max_glossary_cards_per_window}"
        )
    glossary_json = canonical_json(canonical_cards)
    glossary_tokens = int(glossary_token_counter(glossary_json))
    if glossary_tokens < 0:
        raise AuditContractError("Glossary token counter returned a negative value")
    if glossary_tokens > caps.max_glossary_tokens_per_window:
        raise AuditContractError(
            "Glossary token cap exceeded: "
            f"{glossary_tokens} > {caps.max_glossary_tokens_per_window}"
        )

    canonical_blocks = [_canonical_block(block) for block in blocks]
    block_ids = [block["block_id"] for block in canonical_blocks]
    if not block_ids:
        raise AuditContractError("Audit packet must contain at least one block")
    if len(set(block_ids)) != len(block_ids):
        raise AuditContractError("Audit packet block IDs must be unique")
    known_refs = set(card_refs)
    for block in canonical_blocks:
        unknown = sorted(set(block["applicable_glossary_refs"]) - known_refs)
        if unknown:
            raise AuditContractError(
                f"Block {block['block_id']} references unknown glossary cards: {unknown}"
            )

    receipt = _canonical_receipt(protected_receipt)
    neighbors = [_canonical_neighbor(row) for row in context_only_neighbors]
    if len(neighbors) > 2:
        raise AuditContractError("At most one preceding and one following neighbor are allowed")
    positions = [row["position"] for row in neighbors]
    if len(set(positions)) != len(positions):
        raise AuditContractError("Context-only neighbor positions must be unique")
    if any(row["block_id"] in set(block_ids) for row in neighbors):
        raise AuditContractError("Context-only neighbors cannot duplicate audited blocks")

    return {
        "contract_version": INPUT_CONTRACT_VERSION,
        "window_id": window_id,
        "source_language": source_language,
        "target_language": target_language,
        "blocks": canonical_blocks,
        "glossary_cards": canonical_cards,
        "protected_receipt": receipt,
        "context_only_neighbors": neighbors,
    }


def render_audit_messages(
    packet: Mapping[str, Any],
    *,
    caps: AuditPacketCaps,
    glossary_token_counter: Callable[[str], int],
) -> list[dict[str, str]]:
    validate_audit_packet(
        packet,
        caps=caps,
        glossary_token_counter=glossary_token_counter,
    )
    user = AUDIT_USER_PROMPT_TEMPLATE.replace(
        "{{packet_json}}", canonical_json(packet)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def audit_reask_note(errors: Sequence[str]) -> str:
    closed = [str(error) for error in errors[:12]]
    return AUDIT_REASK_NOTE_TEMPLATE.replace(
        "{{errors_json}}", canonical_json(closed)
    )


def render_targeted_repair_note(
    writable_block_ids: Sequence[str],
    issue_summaries: Sequence[Mapping[str, Any]],
) -> str:
    """Render defect evidence without free-form rationale or replacement wording."""

    block_ids = [str(block_id) for block_id in writable_block_ids]
    if not block_ids or any(not block_id for block_id in block_ids):
        raise AuditContractError("Writable repair block IDs must be nonempty")
    if len(set(block_ids)) != len(block_ids):
        raise AuditContractError("Writable repair block IDs must be unique")
    expected_fields = {
        "block_id",
        "issue_type",
        "source_evidence",
        "target_evidence",
    }
    allowed_types = ISSUE_TYPES | frozenset(DETERMINISTIC_ISSUE_SEVERITY)
    canonical_summaries: list[dict[str, str]] = []
    observed: set[tuple[str, str, str, str]] = set()
    covered: set[str] = set()
    for index, raw in enumerate(issue_summaries):
        if not isinstance(raw, Mapping):
            raise AuditContractError(f"Repair issue summary {index} must be an object")
        _require_exact_fields(raw, expected_fields, f"repair issue summary {index}")
        if any(not isinstance(raw.get(key), str) for key in expected_fields):
            raise AuditContractError(f"Repair issue summary {index} fields must be strings")
        block_id = str(raw["block_id"])
        issue_type = str(raw["issue_type"])
        if block_id not in block_ids:
            raise AuditContractError(f"Repair issue summary {index} has foreign block_id")
        if issue_type not in allowed_types:
            raise AuditContractError(f"Repair issue summary {index} has unknown issue_type")
        summary = {
            "block_id": block_id,
            "issue_type": issue_type,
            "source_evidence": str(raw["source_evidence"]),
            "target_evidence": str(raw["target_evidence"]),
        }
        signature = tuple(summary[key] for key in sorted(expected_fields))
        if signature in observed:
            raise AuditContractError(f"Repair issue summary {index} is duplicated")
        observed.add(signature)
        covered.add(block_id)
        canonical_summaries.append(summary)
    missing = [block_id for block_id in block_ids if block_id not in covered]
    if missing:
        raise AuditContractError(f"Writable repair blocks lack issue evidence: {missing}")

    return (
        TARGETED_REPAIR_PROMPT_TEMPLATE.replace(
            "{{writable_block_ids_json}}", canonical_json(block_ids)
        ).replace(
            "{{validated_issue_summaries_json}}", canonical_json(canonical_summaries)
        )
    )


def parse_audit_json(text: str) -> Mapping[str, Any]:
    duplicate_keys: list[str] = []

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                duplicate_keys.append(str(key))
            value[str(key)] = item
        return value

    def reject_constant(value: str) -> None:
        raise AuditContractError(f"Audit JSON contains non-finite number: {value}")

    try:
        parsed = json.loads(
            str(text),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise AuditContractError(f"Audit JSON parse failed: {exc}") from exc
    if duplicate_keys:
        raise AuditContractError(
            "Duplicate audit JSON keys: " + ", ".join(sorted(set(duplicate_keys)))
        )
    if not isinstance(parsed, Mapping):
        raise AuditContractError("Audit response must be one JSON object")
    return parsed


def validate_audit_packet(
    packet: Mapping[str, Any],
    *,
    caps: AuditPacketCaps | None = None,
    glossary_token_counter: Callable[[str], int] | None = None,
) -> None:
    expected = {
        "contract_version",
        "window_id",
        "source_language",
        "target_language",
        "blocks",
        "glossary_cards",
        "protected_receipt",
        "context_only_neighbors",
    }
    _require_exact_fields(packet, expected, "audit packet")
    if packet.get("contract_version") != INPUT_CONTRACT_VERSION:
        raise AuditContractError("Audit packet contract version mismatch")
    if not isinstance(packet.get("window_id"), str) or not packet["window_id"]:
        raise AuditContractError("Audit packet window_id must be nonempty")
    for key in ("source_language", "target_language"):
        if not isinstance(packet.get(key), str) or not packet[key]:
            raise AuditContractError(f"Audit packet {key} must be nonempty")
    blocks = packet.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise AuditContractError("Audit packet blocks must be a nonempty array")
    canonical = [_canonical_block(block) for block in blocks]
    ids = [row["block_id"] for row in canonical]
    if len(set(ids)) != len(ids):
        raise AuditContractError("Audit packet block IDs must be unique")
    cards = packet.get("glossary_cards")
    if not isinstance(cards, list):
        raise AuditContractError("Audit packet glossary_cards must be an array")
    canonical_cards = [_canonical_glossary_card(card) for card in cards]
    refs = [row["glossary_ref"] for row in canonical_cards]
    if len(set(refs)) != len(refs):
        raise AuditContractError("Audit packet glossary refs must be unique")
    if (caps is None) != (glossary_token_counter is None):
        raise AuditContractError(
            "Bounded packet validation requires both caps and glossary token counter"
        )
    if caps is not None and glossary_token_counter is not None:
        caps.validate()
        if len(canonical_cards) > caps.max_glossary_cards_per_window:
            raise AuditContractError("Audit packet exceeds sealed glossary card cap")
        token_count = int(glossary_token_counter(canonical_json(canonical_cards)))
        if token_count < 0:
            raise AuditContractError("Glossary token counter returned a negative value")
        if token_count > caps.max_glossary_tokens_per_window:
            raise AuditContractError("Audit packet exceeds sealed glossary token cap")
    known_refs = set(refs)
    for block in canonical:
        if not set(block["applicable_glossary_refs"]) <= known_refs:
            raise AuditContractError("Audit block references a foreign glossary card")
    _canonical_receipt(packet.get("protected_receipt"))
    neighbors = packet.get("context_only_neighbors")
    if not isinstance(neighbors, list) or len(neighbors) > 2:
        raise AuditContractError("Audit context-only neighbors must contain at most two rows")
    canonical_neighbors = [_canonical_neighbor(row) for row in neighbors]
    positions = [row["position"] for row in canonical_neighbors]
    if len(set(positions)) != len(positions):
        raise AuditContractError("Audit neighbor positions must be unique")
    if any(row["block_id"] in set(ids) for row in canonical_neighbors):
        raise AuditContractError("Audit neighbor duplicates an audited block")


def validate_audit_response(
    payload: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    validate_audit_packet(packet)
    _require_exact_fields(payload, _TOP_LEVEL_FIELDS, "audit response")
    if payload.get("contract_version") != RESPONSE_CONTRACT_VERSION:
        raise AuditContractError("Audit response contract version mismatch")
    if payload.get("window_id") != packet.get("window_id"):
        raise AuditContractError("Audit response window_id mismatch")

    expected_ids = [str(row["block_id"]) for row in packet["blocks"]]
    audited_ids = payload.get("audited_block_ids")
    if not isinstance(audited_ids, list) or any(
        not isinstance(value, str) for value in audited_ids
    ):
        raise AuditContractError("audited_block_ids must be an array of strings")
    if audited_ids != expected_ids:
        raise AuditContractError(
            "audited_block_ids must exact-cover packet blocks in input order"
        )

    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise AuditContractError("Audit findings must be an array")
    text_by_id = {
        str(row["block_id"]): (
            str(row["source_audit_text"]),
            str(row["target_audit_text"]),
        )
        for row in packet["blocks"]
    }
    canonical_findings: list[dict[str, str]] = []
    observed: set[tuple[str, ...]] = set()
    for index, raw in enumerate(findings):
        if not isinstance(raw, Mapping):
            raise AuditContractError(f"Finding {index} must be an object")
        _require_exact_fields(raw, _FINDING_FIELDS, f"finding {index}")
        finding = {key: raw[key] for key in sorted(_FINDING_FIELDS)}
        if any(not isinstance(value, str) for value in finding.values()):
            raise AuditContractError(f"Finding {index} fields must all be strings")
        block_id = finding["block_id"]
        issue_type = finding["issue_type"]
        severity = finding["severity"]
        source_evidence = finding["source_evidence"]
        target_evidence = finding["target_evidence"]
        reason = finding["reason"].strip()
        if block_id not in text_by_id:
            raise AuditContractError(f"Finding {index} references foreign block_id")
        if issue_type not in ISSUE_TYPES:
            raise AuditContractError(f"Finding {index} has unknown issue_type")
        if severity not in SEVERITIES:
            raise AuditContractError(f"Finding {index} has unknown severity")
        if not reason:
            raise AuditContractError(f"Finding {index} reason must be nonempty")
        if issue_type == "style_or_fluency_advisory" and severity != "advisory":
            raise AuditContractError("Style/fluency findings must be advisory")
        source_text, target_text = text_by_id[block_id]
        if source_evidence:
            if source_evidence not in source_text:
                raise AuditContractError(f"Finding {index} source evidence is not exact")
        elif issue_type != "unsupported_addition":
            raise AuditContractError(f"Finding {index} requires source evidence")
        if target_evidence:
            if target_evidence not in target_text:
                raise AuditContractError(f"Finding {index} target evidence is not exact")
        elif issue_type != "meaning_omission":
            raise AuditContractError(f"Finding {index} requires target evidence")
        if issue_type == "semantic_other" and not (source_evidence and target_evidence):
            raise AuditContractError("semantic_other requires source and target evidence")
        signature = tuple(finding[key] for key in sorted(_FINDING_FIELDS))
        if signature in observed:
            raise AuditContractError(f"Finding {index} duplicates an earlier finding")
        observed.add(signature)
        finding["reason"] = reason
        canonical_findings.append(finding)

    return {
        "contract_version": RESPONSE_CONTRACT_VERSION,
        "window_id": str(packet["window_id"]),
        "audited_block_ids": expected_ids,
        "findings": canonical_findings,
    }


def mint_finding_ids(
    packet: Mapping[str, Any],
    validated_response: Mapping[str, Any],
) -> list[dict[str, Any]]:
    validated = validate_audit_response(validated_response, packet)
    packet_digest = content_sha256(packet)
    rows: list[dict[str, Any]] = []
    for finding in validated["findings"]:
        material = {
            "packet_sha256": packet_digest,
            **finding,
        }
        digest = content_sha256(material)[:16].lower()
        rows.append({"finding_id": f"dqaf_{digest}", **finding})
    return rows


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_sha256(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest().upper()


def prompt_sha256() -> str:
    return content_sha256(
        {
            "system_prompt": SYSTEM_PROMPT,
            "user_prompt_template": AUDIT_USER_PROMPT_TEMPLATE,
        }
    )


def audit_reask_policy_sha256() -> str:
    return sha256(AUDIT_REASK_NOTE_TEMPLATE.encode("utf-8")).hexdigest().upper()


def targeted_repair_prompt_sha256() -> str:
    return sha256(TARGETED_REPAIR_PROMPT_TEMPLATE.encode("utf-8")).hexdigest().upper()


def response_schema_sha256() -> str:
    return content_sha256(RESPONSE_JSON_SCHEMA)


def build_semantic_contract_manifest(
    *,
    deterministic_policy_id: str,
    deterministic_policy_sha256: str,
    state_policy_id: str,
    caps: AuditPacketCaps,
    glossary_token_counter_id: str,
) -> dict[str, Any]:
    """Bind pipeline-owned semantic bytes before provider/model sealing."""

    caps.validate()
    for owner, value in (
        ("deterministic_policy_id", deterministic_policy_id),
        ("state_policy_id", state_policy_id),
        ("glossary_token_counter_id", glossary_token_counter_id),
    ):
        if not isinstance(value, str) or not value:
            raise AuditContractError(f"Semantic contract {owner} must be nonempty")
    if not _is_sha256(deterministic_policy_sha256):
        raise AuditContractError("Deterministic policy SHA-256 must be 64 hex characters")
    return {
        "contract_version": SEMANTIC_CONTRACT_MANIFEST_VERSION,
        "input_contract_version": INPUT_CONTRACT_VERSION,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "prompt_id": PROMPT_ID,
        "prompt_sha256": prompt_sha256(),
        "response_schema_id": RESPONSE_SCHEMA_ID,
        "response_schema_sha256": response_schema_sha256(),
        "local_validator_id": LOCAL_VALIDATOR_ID,
        "audit_reask_policy_id": AUDIT_REASK_POLICY_ID,
        "audit_reask_policy_sha256": audit_reask_policy_sha256(),
        "targeted_repair_prompt_id": TARGETED_REPAIR_PROMPT_ID,
        "targeted_repair_prompt_sha256": targeted_repair_prompt_sha256(),
        "deterministic_policy_id": deterministic_policy_id,
        "deterministic_policy_sha256": deterministic_policy_sha256.upper(),
        "state_policy_id": state_policy_id,
        "glossary_token_counter_id": glossary_token_counter_id,
        "packet_caps": {
            "max_glossary_cards_per_window": caps.max_glossary_cards_per_window,
            "max_glossary_tokens_per_window": caps.max_glossary_tokens_per_window,
        },
    }


def validate_semantic_contract_manifest(
    observed: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    if canonical_json(observed) != canonical_json(expected):
        raise AuditContractError("Semantic contract manifest drift")


def _canonical_block(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AuditContractError("Audit block must be an object")
    _require_exact_fields(raw, _BLOCK_FIELDS, "audit block")
    for key in ("block_id", "block_type", "source_audit_text", "target_audit_text"):
        if not isinstance(raw.get(key), str) or (key != "target_audit_text" and not raw[key]):
            raise AuditContractError(f"Audit block {key} must be a valid string")
    refs = raw.get("applicable_glossary_refs")
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        raise AuditContractError("applicable_glossary_refs must be nonempty strings")
    if len(set(refs)) != len(refs):
        raise AuditContractError("applicable_glossary_refs must be unique")
    deterministic = raw.get("deterministic_findings")
    if not isinstance(deterministic, list):
        raise AuditContractError("deterministic_findings must be an array")
    canonical_findings = [
        _canonical_deterministic_finding(row, str(raw["block_id"]))
        for row in deterministic
    ]
    return {
        "block_id": str(raw["block_id"]),
        "block_type": str(raw["block_type"]),
        "source_audit_text": str(raw["source_audit_text"]),
        "target_audit_text": str(raw["target_audit_text"]),
        "applicable_glossary_refs": list(refs),
        "deterministic_findings": canonical_findings,
    }


def _canonical_deterministic_finding(
    raw: Mapping[str, Any], expected_block_id: str
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AuditContractError("Deterministic finding must be an object")
    _require_exact_fields(raw, _DETERMINISTIC_FINDING_FIELDS, "deterministic finding")
    if raw.get("block_id") != expected_block_id:
        raise AuditContractError("Deterministic finding block_id mismatch")
    for key in ("issue_type", "severity", "evidence_source", "evidence_target"):
        if not isinstance(raw.get(key), str):
            raise AuditContractError(f"Deterministic finding {key} must be a string")
    issue_type = str(raw["issue_type"])
    expected_severity = DETERMINISTIC_ISSUE_SEVERITY.get(issue_type)
    if expected_severity is None:
        raise AuditContractError("Deterministic finding issue_type is outside the closed enum")
    if raw["severity"] != expected_severity:
        raise AuditContractError("Deterministic finding severity does not match issue_type")
    if not isinstance(raw.get("details"), Mapping):
        raise AuditContractError("Deterministic finding details must be an object")
    details = dict(raw["details"])
    _require_exact_fields(
        details,
        DETERMINISTIC_DETAILS_FIELDS[issue_type],
        f"deterministic finding {issue_type} details",
    )
    _validate_deterministic_details(issue_type, details)
    try:
        details_bytes = canonical_json(details).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuditContractError("Deterministic finding details must be canonical JSON") from exc
    if len(details_bytes) > MAX_DETERMINISTIC_DETAILS_BYTES:
        raise AuditContractError("Deterministic finding details exceed byte cap")
    return {
        "block_id": expected_block_id,
        "issue_type": issue_type,
        "severity": str(raw["severity"]),
        "evidence_source": str(raw["evidence_source"]),
        "evidence_target": str(raw["evidence_target"]),
        "details": details,
    }


def _validate_deterministic_details(issue_type: str, details: Mapping[str, Any]) -> None:
    string_fields = {
        "forbidden_control_character": {"codepoint"},
        "unexpected_output_script": {"script", "surface"},
        "target_equals_source": {"normalization"},
        "untranslated_heading": {"normalization"},
    }.get(issue_type, set())
    integer_fields = {
        "forbidden_control_character": {"offset"},
        "unexpected_output_script": {"start", "end"},
        "source_language_residue_candidate": {
            "matched_tokens",
            "matched_characters",
            "threshold_tokens",
            "threshold_characters",
        },
        "gross_length_anomaly": {"source_alnum", "target_alnum"},
    }.get(issue_type, set())
    number_fields = (
        {"ratio", "minimum_ratio", "maximum_ratio"}
        if issue_type == "gross_length_anomaly"
        else set()
    )
    for key in string_fields:
        if not isinstance(details.get(key), str) or not details[key]:
            raise AuditContractError(
                f"Deterministic finding {issue_type} detail {key} must be nonempty text"
            )
    for key in integer_fields:
        value = details.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AuditContractError(
                f"Deterministic finding {issue_type} detail {key} must be nonnegative integer"
            )
    for key in number_fields:
        value = details.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise AuditContractError(
                f"Deterministic finding {issue_type} detail {key} must be nonnegative number"
            )


def _canonical_glossary_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AuditContractError("Glossary card must be an object")
    _require_exact_fields(raw, _GLOSSARY_FIELDS, "glossary card")
    for key in ("glossary_ref", "source_term", "policy"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise AuditContractError(f"Glossary card {key} must be nonempty")
    if raw["policy"] not in GLOSSARY_POLICIES:
        raise AuditContractError("Glossary card policy is outside the closed enum")
    variants = raw.get("allowed_target_variants")
    if not isinstance(variants, list) or not variants:
        raise AuditContractError("Glossary card must contain target variants")
    if any(not isinstance(value, str) or not value for value in variants):
        raise AuditContractError("Glossary target variants must be nonempty strings")
    if len(set(variants)) != len(variants):
        raise AuditContractError("Glossary target variants must be unique")
    return {
        "glossary_ref": str(raw["glossary_ref"]),
        "source_term": str(raw["source_term"]),
        "allowed_target_variants": list(variants),
        "policy": str(raw["policy"]),
    }


def _canonical_receipt(raw: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AuditContractError("Protected receipt must be an object")
    _require_exact_fields(raw, _RECEIPT_FIELDS, "protected receipt")
    if not isinstance(raw.get("policy_id"), str) or not raw["policy_id"]:
        raise AuditContractError("Protected receipt policy_id must be nonempty")
    for key in (
        "exact_cover",
        "source_target_ref_order_equal",
        "raw_protected_payload_visible",
    ):
        if not isinstance(raw.get(key), bool):
            raise AuditContractError(f"Protected receipt {key} must be boolean")
    if not raw["exact_cover"] or not raw["source_target_ref_order_equal"]:
        raise AuditContractError("Auditor packet requires a passing protected receipt")
    if raw["raw_protected_payload_visible"]:
        raise AuditContractError("Raw protected payload must not be visible to Auditor")
    return {key: raw[key] for key in sorted(_RECEIPT_FIELDS)}


def _canonical_neighbor(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AuditContractError("Context-only neighbor must be an object")
    _require_exact_fields(raw, _NEIGHBOR_FIELDS, "context-only neighbor")
    if raw.get("position") not in {"preceding", "following"}:
        raise AuditContractError("Neighbor position must be preceding or following")
    for key in ("block_id", "source_audit_text", "target_audit_text"):
        if not isinstance(raw.get(key), str) or (key == "block_id" and not raw[key]):
            raise AuditContractError(f"Neighbor {key} must be a valid string")
    return {key: raw[key] for key in sorted(_NEIGHBOR_FIELDS)}


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], owner: str
) -> None:
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise AuditContractError(
            f"{owner} fields mismatch; missing={missing}; extra={extra}"
        )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


__all__ = [
    "AuditContractError",
    "AuditPacketCaps",
    "AUDIT_REASK_NOTE_TEMPLATE",
    "AUDIT_REASK_POLICY_ID",
    "AUDIT_USER_PROMPT_TEMPLATE",
    "DETERMINISTIC_ISSUE_SEVERITY",
    "DETERMINISTIC_DETAILS_FIELDS",
    "GLOSSARY_POLICIES",
    "INPUT_CONTRACT_VERSION",
    "ISSUE_TYPES",
    "PRESET_ID",
    "PROMPT_ID",
    "RESPONSE_SCHEMA_ID",
    "LOCAL_VALIDATOR_ID",
    "SEMANTIC_CONTRACT_MANIFEST_VERSION",
    "TARGETED_REPAIR_PROMPT_ID",
    "RESPONSE_CONTRACT_VERSION",
    "RESPONSE_JSON_SCHEMA",
    "ROLE_ID",
    "SEVERITIES",
    "SYSTEM_PROMPT",
    "TARGETED_REPAIR_PROMPT_TEMPLATE",
    "audit_reask_note",
    "audit_reask_policy_sha256",
    "build_semantic_contract_manifest",
    "build_audit_packet",
    "canonical_json",
    "content_sha256",
    "mint_finding_ids",
    "parse_audit_json",
    "prompt_sha256",
    "response_schema_sha256",
    "render_audit_messages",
    "render_targeted_repair_note",
    "targeted_repair_prompt_sha256",
    "validate_audit_packet",
    "validate_audit_response",
    "validate_semantic_contract_manifest",
]
