"""B4 Editorial Review: deterministic routing, LLM critique, and approved apply."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from pipeline.literary.b4_translator_pack_v1 import (
    B4TranslatorPackError,
    verify_translator_pack_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


ROLE_ID = "literary.b4.editorial_review"
PROMPT_ID = "literary_b4_editorial_review_v1"
PACKET_SCHEMA_VERSION = "literary_b4_editorial_review_packet_v1"
SELECTION_REPORT_SCHEMA_VERSION = (
    "literary_b4_editorial_selection_report_v1"
)
RESPONSE_SCHEMA_VERSION = "literary_b4_editorial_review_response_v1"
VALIDATED_RESPONSE_SCHEMA_VERSION = (
    "literary_b4_validated_editorial_review_v1"
)
REVIEW_ARTIFACT_SCHEMA_VERSION = (
    "literary_b4_editorial_review_artifact_v1"
)
APPROVAL_SCHEMA_VERSION = "literary_b4_editorial_approval_v1"
EDITED_TRANSLATION_SCHEMA_VERSION = (
    "literary_b4_translation_editorially_edited_v1"
)

SELECTION_MODES = frozenset(
    {"all_blocks", "flagged_only", "flagged_plus_sample"}
)
ISSUE_TYPES = frozenset(
    {
        "omission",
        "addition",
        "mistranslation",
        "style",
        "fluency",
        "discourse",
        "narrative_quality",
        "terminology",
        "entity",
        "formula",
        "source_carry_through",
        "encoding",
        "typography",
    }
)
SEVERITIES = frozenset({"critical", "major", "minor", "suggestion"})
ACTIONS = frozenset({"accept", "repair", "human_review"})

SYSTEM_PROMPT = """You are the Tier-2 Critic for an English-to-Vietnamese literary translation.

Review only the candidate blocks supplied in EDITORIAL_REVIEW_PACKET. The
English source is authoritative for what is said. The B4 context is
authoritative only for identity, terminology, address, and established
narrative facts. The style profile guides Vietnamese prose but never overrides
the source.

For each candidate, compare source_text with current_target_text. Check
omission, addition, mistranslation, terminology, entity, discourse, narrative
quality, style, fluency, formula/symbol preservation, encoding, typography,
and source-language carry-through. Neighbor rows are context only and must
never be revised.

Return every candidate exactly once and in the supplied order. Use:
- accept: keep proposed_target_text byte-identical to current_target_text.
- repair: change only what is needed; proposed_target_text must be a complete
  replacement for that one block.
- human_review: keep proposed_target_text byte-identical to the current text
  when the ambiguity cannot be resolved safely from supplied evidence.

Every issue must quote verbatim evidence from the supplied source and/or
current target. Do not consult, imitate, or mention any published or human
reference translation. Do not modify entity, relation, state, or Story Bible
records. Return JSON only."""


class B4EditorialReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedEditorialReviewV1:
    packet: Mapping[str, Any]
    messages: tuple[Mapping[str, Any], ...]
    response_schema: Mapping[str, Any]
    request_fingerprint: str


def build_editorial_review_packets_v1(
    *,
    translation_artifact: Mapping[str, Any],
    chapter: Mapping[str, Any],
    translator_pack: Mapping[str, Any],
    lint_report: Mapping[str, Any],
    style_profile_version: str,
    style_profile_sha256: str,
    selection_mode: str,
    explicit_block_ids: Sequence[str] = (),
    sample_count: int = 0,
    sample_seed: str = "",
    context_radius: int = 1,
    max_candidates_per_batch: int = 8,
    window_slices: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    translation = _verify_translation(translation_artifact)
    source_rows = _verify_source_chapter(
        chapter=chapter,
        translation=translation,
    )
    pack = _verify_pack(
        translator_pack=translator_pack,
        translation=translation,
    )
    lint = _verify_lint_report(
        lint_report=lint_report,
        translation=translation,
    )
    if selection_mode not in SELECTION_MODES:
        raise B4EditorialReviewError("unsupported editorial selection mode")
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count < 0
    ):
        raise B4EditorialReviewError("sample_count must be a non-negative int")
    if selection_mode != "flagged_plus_sample" and sample_count:
        raise B4EditorialReviewError(
            "sample_count is only valid for flagged_plus_sample"
        )
    if selection_mode == "flagged_plus_sample" and sample_count <= 0:
        raise B4EditorialReviewError(
            "flagged_plus_sample requires a positive sample_count"
        )
    if (
        not isinstance(context_radius, int)
        or isinstance(context_radius, bool)
        or context_radius < 0
        or context_radius > 3
    ):
        raise B4EditorialReviewError(
            "context_radius must be an integer from 0 through 3"
        )
    if (
        not isinstance(max_candidates_per_batch, int)
        or isinstance(max_candidates_per_batch, bool)
        or max_candidates_per_batch <= 0
        or max_candidates_per_batch > 24
    ):
        raise B4EditorialReviewError(
            "max_candidates_per_batch must be from 1 through 24"
        )
    style_version = _text(style_profile_version, "style_profile_version")
    _hash_text(style_profile_sha256, "style_profile_sha256")

    slices = _verify_window_slices(
        window_slices=window_slices,
        chapter_id=str(translation["chapter_id"]),
    )
    block_order = [row["block_id"] for row in source_rows]
    source_by_id = {row["block_id"]: row for row in source_rows}
    translation_by_id = {
        str(row["block_id"]): dict(row) for row in translation["blocks"]
    }
    reasons = _selection_reasons(
        lint=lint,
        block_order=block_order,
        explicit_block_ids=explicit_block_ids,
    )
    if selection_mode == "all_blocks":
        for block_id in block_order:
            reasons[block_id].add("full_review")
    elif selection_mode == "flagged_plus_sample":
        remaining = [
            block_id for block_id in block_order if not reasons[block_id]
        ]
        ranked = sorted(
            remaining,
            key=lambda block_id: (
                canonical_hash(
                    {
                        "sample_seed": sample_seed,
                        "chapter_id": translation["chapter_id"],
                        "block_id": block_id,
                    }
                ),
                block_id,
            ),
        )
        for block_id in ranked[:sample_count]:
            reasons[block_id].add("deterministic_sample")

    selected_ids = [block_id for block_id in block_order if reasons[block_id]]
    tier1_by_block = _tier1_findings_by_block(lint)
    batches = [
        selected_ids[index : index + max_candidates_per_batch]
        for index in range(0, len(selected_ids), max_candidates_per_batch)
    ]
    packets: list[dict[str, Any]] = []
    for batch_index, candidate_ids in enumerate(batches, start=1):
        context_ids = _context_block_ids(
            block_order=block_order,
            candidate_ids=candidate_ids,
            radius=context_radius,
        )
        candidate_rows = []
        for block_id in candidate_ids:
            candidate_rows.append(
                {
                    "block_id": block_id,
                    "block_order": block_order.index(block_id) + 1,
                    "source_text": source_by_id[block_id]["source_text"],
                    "current_target_text": translation_by_id[block_id][
                        "target_text"
                    ],
                    "selection_reasons": sorted(reasons[block_id]),
                    "tier1_findings": deepcopy(
                        tier1_by_block.get(block_id, [])
                    ),
                }
            )
        neighbor_rows = [
            {
                "block_id": block_id,
                "block_order": block_order.index(block_id) + 1,
                "source_text": source_by_id[block_id]["source_text"],
                "current_target_text": translation_by_id[block_id][
                    "target_text"
                ],
                "candidate": block_id in set(candidate_ids),
            }
            for block_id in context_ids
        ]
        pack_context = _project_pack_context(
            translator_pack=pack,
            source_rows=source_rows,
            translation_rows=translation["blocks"],
            context_block_ids=context_ids,
            window_slices=slices,
        )
        packet_body = {
            "schema_version": PACKET_SCHEMA_VERSION,
            "book_id": translation.get("book_id"),
            "chapter_id": translation["chapter_id"],
            "batch_index": batch_index,
            "batch_count": len(batches),
            "selection_mode": selection_mode,
            "source_translation_artifact_hash": translation["artifact_hash"],
            "translator_pack_artifact_hash": pack["artifact_hash"],
            "lint_report_artifact_hash": lint["artifact_hash"],
            "style_profile_version": style_version,
            "style_profile_sha256": style_profile_sha256,
            "candidate_block_ids": list(candidate_ids),
            "candidates": candidate_rows,
            "neighbor_context": neighbor_rows,
            "pack_context": pack_context,
            "provider_calls": 0,
            "semantic_record_mutation_performed": False,
        }
        packets.append(
            {
                **packet_body,
                "artifact_hash": canonical_hash(packet_body),
            }
        )

    reason_counts = Counter(
        reason
        for block_id in selected_ids
        for reason in reasons[block_id]
    )
    report_body = {
        "schema_version": SELECTION_REPORT_SCHEMA_VERSION,
        "status": "ready" if selected_ids else "no_candidates",
        "book_id": translation.get("book_id"),
        "chapter_id": translation["chapter_id"],
        "source_translation_artifact_hash": translation["artifact_hash"],
        "translator_pack_artifact_hash": pack["artifact_hash"],
        "lint_report_artifact_hash": lint["artifact_hash"],
        "selection_mode": selection_mode,
        "source_block_count": len(block_order),
        "candidate_block_count": len(selected_ids),
        "candidate_block_ids": selected_ids,
        "selection_reason_counts": dict(sorted(reason_counts.items())),
        "batch_count": len(packets),
        "max_candidates_per_batch": max_candidates_per_batch,
        "context_radius": context_radius,
        "sample_count_requested": sample_count,
        "sample_seed": sample_seed if selection_mode == "flagged_plus_sample" else None,
        "provider_calls": 0,
        "translation_text_mutation_performed": False,
        "semantic_record_mutation_performed": False,
    }
    report = {**report_body, "artifact_hash": canonical_hash(report_body)}
    return packets, report


def editorial_review_response_schema_v1(
    candidate_block_ids: Sequence[str],
) -> dict[str, Any]:
    ids = [_text(value, "candidate_block_id") for value in candidate_block_ids]
    if not ids or len(ids) != len(set(ids)):
        raise B4EditorialReviewError(
            "editorial response schema requires unique candidates"
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "blocks"],
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": [RESPONSE_SCHEMA_VERSION],
            },
            "blocks": {
                "type": "array",
                "minItems": len(ids),
                "maxItems": len(ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "block_id",
                        "quality_score",
                        "suggested_action",
                        "proposed_target_text",
                        "issues",
                    ],
                    "properties": {
                        "block_id": {"type": "string", "enum": ids},
                        "quality_score": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "suggested_action": {
                            "type": "string",
                            "enum": sorted(ACTIONS),
                        },
                        "proposed_target_text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 20_000,
                        },
                        "issues": {
                            "type": "array",
                            "maxItems": 12,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "type",
                                    "severity",
                                    "description",
                                    "evidence_source",
                                    "evidence_target",
                                    "suggested_fix",
                                ],
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": sorted(ISSUE_TYPES),
                                    },
                                    "severity": {
                                        "type": "string",
                                        "enum": sorted(SEVERITIES),
                                    },
                                    "description": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 800,
                                    },
                                    "evidence_source": {
                                        "type": "string",
                                        "maxLength": 1_200,
                                    },
                                    "evidence_target": {
                                        "type": "string",
                                        "maxLength": 1_200,
                                    },
                                    "suggested_fix": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 1_200,
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def render_editorial_review_request_v1(
    *,
    review_packet: Mapping[str, Any],
    style_profile: str,
) -> RenderedEditorialReviewV1:
    packet = _verify_packet(review_packet)
    profile = _text(style_profile, "style_profile")
    if canonical_hash(profile) != packet["style_profile_sha256"]:
        raise B4EditorialReviewError(
            "Editorial Review style profile hash differs"
        )
    response_schema = editorial_review_response_schema_v1(
        packet["candidate_block_ids"]
    )
    messages = (
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "STYLE_PROFILE\n"
                + profile
                + "\n\nEDITORIAL_REVIEW_PACKET\n"
                + canonical_json(packet)
            ),
        },
    )
    request_fingerprint = canonical_hash(
        {
            "prompt_id": PROMPT_ID,
            "messages": messages,
            "response_schema": response_schema,
        }
    )
    return RenderedEditorialReviewV1(
        packet=packet,
        messages=messages,
        response_schema=response_schema,
        request_fingerprint=request_fingerprint,
    )


def validate_editorial_review_response_v1(
    *,
    rendered: RenderedEditorialReviewV1,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(response))
    if raw.get("schema_version") != RESPONSE_SCHEMA_VERSION:
        raise B4EditorialReviewError(
            "unsupported Editorial Review response schema"
        )
    rows = raw.get("blocks")
    if not isinstance(rows, list):
        raise B4EditorialReviewError(
            "Editorial Review response blocks must be a list"
        )
    expected_ids = list(rendered.packet["candidate_block_ids"])
    received_ids = [
        _text(row.get("block_id"), "review block_id")
        if isinstance(row, Mapping)
        else ""
        for row in rows
    ]
    if received_ids != expected_ids:
        raise B4EditorialReviewError(
            "Editorial Review response must exact-cover candidates in order"
        )
    candidates = {
        str(row["block_id"]): row for row in rendered.packet["candidates"]
    }
    normalized_rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise B4EditorialReviewError("Editorial Review row is malformed")
        block_id = str(row["block_id"])
        candidate = candidates[block_id]
        source_text = str(candidate["source_text"])
        current_target = str(candidate["current_target_text"])
        score = row.get("quality_score")
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0 <= float(score) <= 1
        ):
            raise B4EditorialReviewError(
                "Editorial Review quality_score is invalid"
            )
        action = str(row.get("suggested_action") or "")
        if action not in ACTIONS:
            raise B4EditorialReviewError(
                "Editorial Review suggested_action is invalid"
            )
        proposed = _text(
            row.get("proposed_target_text"),
            "proposed_target_text",
        )
        issues = row.get("issues")
        if not isinstance(issues, list) or len(issues) > 12:
            raise B4EditorialReviewError(
                "Editorial Review issues are malformed"
            )
        normalized_issues = []
        for issue in issues:
            normalized_issues.append(
                _normalize_issue(
                    block_id=block_id,
                    issue=issue,
                    source_text=source_text,
                    target_text=current_target,
                )
            )
        serious = {
            issue["severity"]
            for issue in normalized_issues
            if issue["severity"] in {"critical", "major"}
        }
        if action == "repair":
            if proposed == current_target or not normalized_issues:
                raise B4EditorialReviewError(
                    "repair must change text and cite at least one issue"
                )
        else:
            if proposed != current_target:
                raise B4EditorialReviewError(
                    "accept and human_review must preserve current text"
                )
        if action == "human_review" and not normalized_issues:
            raise B4EditorialReviewError(
                "human_review must cite at least one issue"
            )
        if action == "accept" and serious:
            raise B4EditorialReviewError(
                "accept cannot retain a major or critical issue"
            )
        normalized_rows.append(
            {
                "block_id": block_id,
                "quality_score": round(float(score), 6),
                "suggested_action": action,
                "current_target_text": current_target,
                "proposed_target_text": proposed,
                "issues": normalized_issues,
            }
        )
    body = {
        "schema_version": VALIDATED_RESPONSE_SCHEMA_VERSION,
        "chapter_id": rendered.packet["chapter_id"],
        "batch_index": rendered.packet["batch_index"],
        "request_fingerprint": rendered.request_fingerprint,
        "review_packet_artifact_hash": rendered.packet["artifact_hash"],
        "source_translation_artifact_hash": rendered.packet[
            "source_translation_artifact_hash"
        ],
        "blocks": normalized_rows,
        "semantic_record_mutation_performed": False,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def build_editorial_review_artifact_v1(
    *,
    rendered: RenderedEditorialReviewV1,
    validated_response: Mapping[str, Any],
    provider_receipt: Mapping[str, Any] | None,
    provider_called: bool,
) -> dict[str, Any]:
    validated = _verify_validated_response(
        validated_response=validated_response,
        rendered=rendered,
    )
    if provider_called is not (provider_receipt is not None):
        raise B4EditorialReviewError(
            "Editorial Review provider receipt state is inconsistent"
        )
    action_counts = Counter(
        str(row["suggested_action"]) for row in validated["blocks"]
    )
    severity_counts = Counter(
        str(issue["severity"])
        for row in validated["blocks"]
        for issue in row["issues"]
    )
    body = {
        "schema_version": REVIEW_ARTIFACT_SCHEMA_VERSION,
        "book_id": rendered.packet.get("book_id"),
        "chapter_id": rendered.packet["chapter_id"],
        "batch_index": rendered.packet["batch_index"],
        "batch_count": rendered.packet["batch_count"],
        "source_translation_artifact_hash": rendered.packet[
            "source_translation_artifact_hash"
        ],
        "translator_pack_artifact_hash": rendered.packet[
            "translator_pack_artifact_hash"
        ],
        "lint_report_artifact_hash": rendered.packet[
            "lint_report_artifact_hash"
        ],
        "review_packet_artifact_hash": rendered.packet["artifact_hash"],
        "request_fingerprint": rendered.request_fingerprint,
        "style_profile_version": rendered.packet["style_profile_version"],
        "candidate_block_ids": list(rendered.packet["candidate_block_ids"]),
        "blocks": deepcopy(validated["blocks"]),
        "action_counts": dict(sorted(action_counts.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "provider_called": provider_called,
        "provider_receipt": (
            deepcopy(dict(provider_receipt))
            if provider_receipt is not None
            else None
        ),
        "translation_text_mutation_performed": False,
        "semantic_record_mutation_performed": False,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def apply_approved_editorial_reviews_v1(
    *,
    translation_artifact: Mapping[str, Any],
    review_artifacts: Sequence[Mapping[str, Any]],
    approval_artifact: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    translation = _verify_translation(translation_artifact)
    reviews = [
        _verify_review_artifact(
            review_artifact=value,
            translation=translation,
        )
        for value in review_artifacts
    ]
    if not reviews:
        raise B4EditorialReviewError(
            "at least one Editorial Review artifact is required"
        )
    seen_blocks: set[str] = set()
    repair_rows: dict[tuple[str, str], Mapping[str, Any]] = {}
    for review in reviews:
        for row in review["blocks"]:
            block_id = str(row["block_id"])
            if block_id in seen_blocks:
                raise B4EditorialReviewError(
                    "Editorial Review artifacts repeat a block"
                )
            seen_blocks.add(block_id)
            if row["suggested_action"] == "repair":
                repair_rows[(review["artifact_hash"], block_id)] = row

    approval = _verify_approval(
        approval_artifact=approval_artifact,
        translation=translation,
        reviews=reviews,
        repair_rows=repair_rows,
    )
    decisions = {
        (row["review_artifact_hash"], row["block_id"]): row["decision"]
        for row in approval["decisions"]
    }
    approved = {
        key: row
        for key, row in repair_rows.items()
        if decisions.get(key) == "approve"
    }
    rejected = {
        key: row
        for key, row in repair_rows.items()
        if decisions.get(key) == "reject"
    }
    pending = {
        key: row
        for key, row in repair_rows.items()
        if key not in decisions
    }
    approved_by_block = {
        block_id: (review_hash, row)
        for (review_hash, block_id), row in approved.items()
    }
    edited_rows = []
    changes = []
    for row in translation["blocks"]:
        block_id = str(row["block_id"])
        edited = deepcopy(dict(row))
        if block_id in approved_by_block:
            review_hash, review_row = approved_by_block[block_id]
            original = str(edited["target_text"])
            revised = str(review_row["proposed_target_text"])
            edited["target_text"] = revised
            changes.append(
                {
                    "block_id": block_id,
                    "review_artifact_hash": review_hash,
                    "original_target_text": original,
                    "revised_target_text": revised,
                    "issue_ids": [
                        issue["issue_id"] for issue in review_row["issues"]
                    ],
                }
            )
        edited_rows.append(edited)

    edited_body = {
        **{
            key: deepcopy(value)
            for key, value in translation.items()
            if key not in {"schema_version", "artifact_hash", "blocks"}
        },
        "schema_version": EDITED_TRANSLATION_SCHEMA_VERSION,
        "source_translation_schema_version": translation["schema_version"],
        "source_translation_artifact_hash": translation["artifact_hash"],
        "editorial_review_artifact_hashes": [
            row["artifact_hash"] for row in reviews
        ],
        "editorial_approval_artifact_hash": approval["artifact_hash"],
        "blocks": edited_rows,
        "editorial_changes": changes,
        "editorial_change_count": len(changes),
        "translation_text_mutation_performed": bool(changes),
        "semantic_record_mutation_performed": False,
        "editorial_provider_calls": sum(
            1 for row in reviews if row["provider_called"]
        ),
    }
    edited = {
        **edited_body,
        "artifact_hash": canonical_hash(edited_body),
    }
    report_body = {
        "schema_version": "literary_b4_editorial_apply_report_v1",
        "status": "complete",
        "book_id": translation.get("book_id"),
        "chapter_id": translation["chapter_id"],
        "source_translation_artifact_hash": translation["artifact_hash"],
        "edited_translation_artifact_hash": edited["artifact_hash"],
        "review_artifact_count": len(reviews),
        "repair_proposal_count": len(repair_rows),
        "approved_revision_count": len(approved),
        "rejected_revision_count": len(rejected),
        "pending_revision_count": len(pending),
        "human_review_block_count": sum(
            1
            for review in reviews
            for row in review["blocks"]
            if row["suggested_action"] == "human_review"
        ),
        "approved_block_ids": sorted(
            block_id for _review_hash, block_id in approved
        ),
        "rejected_block_ids": sorted(
            block_id for _review_hash, block_id in rejected
        ),
        "pending_block_ids": sorted(
            block_id for _review_hash, block_id in pending
        ),
        "provider_calls": 0,
        "semantic_record_mutation_performed": False,
    }
    report = {**report_body, "artifact_hash": canonical_hash(report_body)}
    return edited, report


def _selection_reasons(
    *,
    lint: Mapping[str, Any],
    block_order: Sequence[str],
    explicit_block_ids: Sequence[str],
) -> dict[str, set[str]]:
    reasons = {block_id: set() for block_id in block_order}
    for issue in lint.get("issues") or []:
        if not isinstance(issue, Mapping):
            raise B4EditorialReviewError("lint issue is malformed")
        block_id = _text(issue.get("block_id"), "lint issue block_id")
        if block_id not in reasons:
            raise B4EditorialReviewError(
                "lint issue cites a foreign translation block"
            )
        kind = _text(issue.get("issue_kind"), "lint issue kind")
        reasons[block_id].add(f"lint_issue:{kind}")
    for observation in lint.get("observations") or []:
        if not isinstance(observation, Mapping):
            raise B4EditorialReviewError("lint observation is malformed")
        block_id = _text(
            observation.get("block_id"),
            "lint observation block_id",
        )
        if block_id not in reasons:
            raise B4EditorialReviewError(
                "lint observation cites a foreign translation block"
            )
        kind = _text(
            observation.get("observation_kind"),
            "lint observation kind",
        )
        reasons[block_id].add(f"lint_observation:{kind}")
    for block_id in explicit_block_ids:
        value = _text(block_id, "explicit block_id")
        if value not in reasons:
            raise B4EditorialReviewError(
                "explicit Editorial Review block is absent"
            )
        reasons[value].add("explicit_request")
    return reasons


def _tier1_findings_by_block(
    lint: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    findings: dict[str, list[dict[str, Any]]] = {}
    for label, rows, kind_key in (
        ("issue", lint.get("issues") or [], "issue_kind"),
        (
            "observation",
            lint.get("observations") or [],
            "observation_kind",
        ),
    ):
        for row in rows:
            block_id = str(row["block_id"])
            findings.setdefault(block_id, []).append(
                {
                    "finding_class": label,
                    "kind": str(row[kind_key]),
                    "evidence": deepcopy(dict(row)),
                }
            )
    for rows in findings.values():
        rows.sort(
            key=lambda row: (
                row["finding_class"],
                row["kind"],
                canonical_hash(row),
            )
        )
    return findings


def _context_block_ids(
    *,
    block_order: Sequence[str],
    candidate_ids: Sequence[str],
    radius: int,
) -> list[str]:
    indexes = {block_id: index for index, block_id in enumerate(block_order)}
    selected: set[int] = set()
    for block_id in candidate_ids:
        index = indexes[block_id]
        selected.update(
            range(
                max(0, index - radius),
                min(len(block_order), index + radius + 1),
            )
        )
    return [block_order[index] for index in sorted(selected)]


def _project_pack_context(
    *,
    translator_pack: Mapping[str, Any],
    source_rows: Sequence[Mapping[str, Any]],
    translation_rows: Sequence[Mapping[str, Any]],
    context_block_ids: Sequence[str],
    window_slices: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    block_ids = set(context_block_ids)
    source_by_id = {
        str(row["block_id"]): str(row["source_text"]) for row in source_rows
    }
    target_by_id = {
        str(row["block_id"]): str(row["target_text"])
        for row in translation_rows
    }
    context_text = "\n".join(
        source_by_id[block_id] + "\n" + target_by_id[block_id]
        for block_id in context_block_ids
    ).casefold()
    relevant_ids: set[str] = set()
    entities = [
        dict(row)
        for row in translator_pack.get("entities") or []
        if isinstance(row, Mapping)
    ]
    for entity in entities:
        entity_id = entity.get("effective_entity_id")
        if not isinstance(entity_id, str):
            continue
        surfaces = []
        for field in ("canonical_surface", "stable_surfaces", "aliases"):
            raw = entity.get(field)
            values = raw if isinstance(raw, list) else [raw]
            surfaces.extend(value for value in values if isinstance(value, str))
        if any(
            len(surface.strip()) >= 2
            and surface.strip().casefold() in context_text
            for surface in surfaces
        ):
            relevant_ids.add(entity_id)

    turn_rows = []
    pair_rows = []
    relevant_turn_ids: set[str] = set()
    for window in window_slices:
        for row in window.get("speaker_turns") or []:
            if (
                isinstance(row, Mapping)
                and row.get("block_id") in block_ids
            ):
                normalized = deepcopy(dict(row))
                turn_rows.append(normalized)
                if isinstance(row.get("speaker_turn_id"), str):
                    relevant_turn_ids.add(str(row["speaker_turn_id"]))
                relevant_ids.update(_effective_entity_ids(row))
        for row in window.get("address_pairs") or []:
            if not isinstance(row, Mapping):
                continue
            turn_ids = {
                str(value)
                for value in row.get("turn_ids") or []
                if isinstance(value, str)
            }
            source_ids = {
                str(value)
                for value in row.get("source_block_ids") or []
                if isinstance(value, str)
            }
            if turn_ids & relevant_turn_ids or source_ids & block_ids:
                pair_rows.append(deepcopy(dict(row)))
                relevant_ids.update(_effective_entity_ids(row))

    relations = []
    for row in translator_pack.get("relations") or []:
        if not isinstance(row, Mapping):
            continue
        source_id = row.get("source_effective_entity_id")
        target_id = row.get("target_effective_entity_id")
        if source_id in relevant_ids or target_id in relevant_ids:
            relations.append(deepcopy(dict(row)))
            if isinstance(source_id, str):
                relevant_ids.add(source_id)
            if isinstance(target_id, str):
                relevant_ids.add(target_id)

    projected_entities = [
        deepcopy(entity)
        for entity in entities
        if entity.get("effective_entity_id") in relevant_ids
    ]
    states = [
        deepcopy(dict(row))
        for row in translator_pack.get("states") or []
        if isinstance(row, Mapping)
        and _effective_entity_ids(row) & relevant_ids
    ]
    idiolect = [
        deepcopy(dict(row))
        for row in translator_pack.get("idiolect") or []
        if isinstance(row, Mapping)
        and row.get("effective_entity_id") in relevant_ids
    ]
    narrative = translator_pack.get("narrative_position") or {}
    frames = [
        deepcopy(dict(row))
        for row in narrative.get("frames") or []
        if isinstance(row, Mapping)
        and _frame_intersects_blocks(row, block_ids, source_rows)
    ]
    capsules = [
        deepcopy(dict(row))
        for row in narrative.get("capsules") or []
        if isinstance(row, Mapping)
        and (
            _effective_entity_ids(row) & relevant_ids
            or any(
                isinstance(value, str)
                and value.casefold() in context_text
                for value in row.get("entity_refs") or []
            )
        )
    ]
    open_questions = {}
    for key, rows in (translator_pack.get("open_questions") or {}).items():
        if not isinstance(rows, list):
            continue
        open_questions[str(key)] = [
            deepcopy(dict(row))
            for row in rows
            if isinstance(row, Mapping)
            and (
                _effective_entity_ids(row) & relevant_ids
                or _contains_any_string(row, block_ids)
            )
        ]
    turn_rows.sort(key=lambda row: str(row.get("speaker_turn_id") or ""))
    pair_rows.sort(key=lambda row: str(row.get("pair_id") or ""))
    return {
        "schema_version": "literary_b4_editorial_pack_context_v1",
        "source_translator_pack_artifact_hash": translator_pack["artifact_hash"],
        "effective_entity_ids": sorted(relevant_ids),
        "entities": projected_entities,
        "relations": relations,
        "states": states,
        "idiolect": idiolect,
        "narrative_position": {
            "frames": frames,
            "capsules": capsules,
        },
        "open_questions": open_questions,
        "speaker_turns": turn_rows,
        "address_pairs": pair_rows,
    }


def _effective_entity_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                isinstance(key, str)
                and (
                    key.endswith("effective_entity_id")
                    or key.endswith("effective_entity_ids")
                    or key in {
                        "subject_referent_refs",
                        "counterpart_referent_refs",
                        "entity_ids",
                        "entity_refs",
                    }
                )
            ):
                values = child if isinstance(child, list) else [child]
                ids.update(
                    item
                    for item in values
                    if isinstance(item, str) and item.startswith("b0ent_")
                )
            ids.update(_effective_entity_ids(child))
    elif isinstance(value, list):
        for child in value:
            ids.update(_effective_entity_ids(child))
    return ids


def _contains_any_string(value: Any, needles: set[str]) -> bool:
    if isinstance(value, str):
        return value in needles
    if isinstance(value, Mapping):
        return any(_contains_any_string(child, needles) for child in value.values())
    if isinstance(value, list):
        return any(_contains_any_string(child, needles) for child in value)
    return False


def _frame_intersects_blocks(
    row: Mapping[str, Any],
    block_ids: set[str],
    source_rows: Sequence[Mapping[str, Any]],
) -> bool:
    order = {
        str(source["block_id"]): index for index, source in enumerate(source_rows)
    }
    start = row.get("start_block_id")
    end = row.get("end_block_id")
    if start not in order or end not in order:
        return False
    lower, upper = sorted((order[str(start)], order[str(end)]))
    return any(
        block_id in order and lower <= order[block_id] <= upper
        for block_id in block_ids
    )


def _normalize_issue(
    *,
    block_id: str,
    issue: Any,
    source_text: str,
    target_text: str,
) -> dict[str, Any]:
    if not isinstance(issue, Mapping):
        raise B4EditorialReviewError("Editorial Review issue is malformed")
    issue_type = str(issue.get("type") or "")
    severity = str(issue.get("severity") or "")
    if issue_type not in ISSUE_TYPES or severity not in SEVERITIES:
        raise B4EditorialReviewError(
            "Editorial Review issue type or severity is invalid"
        )
    description = _bounded_text(
        issue.get("description"),
        "issue description",
        maximum=800,
    )
    evidence_source = _optional_bounded_text(
        issue.get("evidence_source"),
        "issue evidence_source",
        maximum=1_200,
    )
    evidence_target = _optional_bounded_text(
        issue.get("evidence_target"),
        "issue evidence_target",
        maximum=1_200,
    )
    suggested_fix = _bounded_text(
        issue.get("suggested_fix"),
        "issue suggested_fix",
        maximum=1_200,
    )
    if not evidence_source and not evidence_target:
        raise B4EditorialReviewError(
            "Editorial Review issue cites no evidence"
        )
    if evidence_source and evidence_source not in source_text:
        raise B4EditorialReviewError(
            "Editorial Review issue source evidence is not verbatim"
        )
    if evidence_target and evidence_target not in target_text:
        raise B4EditorialReviewError(
            "Editorial Review issue target evidence is not verbatim"
        )
    issue_body = {
        "block_id": block_id,
        "type": issue_type,
        "severity": severity,
        "description": description,
        "evidence": {
            "source": evidence_source,
            "target": evidence_target,
        },
        "suggested_fix": suggested_fix,
        "detected_by": "llm_reviewer",
    }
    return {
        "issue_id": f"b4edit1_{canonical_hash(issue_body)[:20]}",
        **issue_body,
    }


def _verify_translation(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = _verify_hashed(value, "translation")
    schema = raw.get("schema_version")
    if not isinstance(schema, str) or not schema.startswith(
        "literary_b4_translation_"
    ):
        raise B4EditorialReviewError(
            "unsupported translation artifact schema"
        )
    _text(raw.get("chapter_id"), "translation chapter_id")
    rows = raw.get("blocks")
    if not isinstance(rows, list) or not rows:
        raise B4EditorialReviewError("translation blocks are malformed")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise B4EditorialReviewError("translation block is malformed")
        block_id = _text(row.get("block_id"), "translation block_id")
        if block_id in seen:
            raise B4EditorialReviewError("translation repeats a block")
        seen.add(block_id)
        _text(row.get("source_text"), "translation source_text")
        _text(row.get("target_text"), "translation target_text")
    return raw


def _verify_source_chapter(
    *,
    chapter: Mapping[str, Any],
    translation: Mapping[str, Any],
) -> list[dict[str, str]]:
    if chapter.get("chapter_id") != translation.get("chapter_id"):
        raise B4EditorialReviewError(
            "Editorial Review source and translation chapters differ"
        )
    rows = chapter.get("blocks")
    if not isinstance(rows, list) or not rows:
        raise B4EditorialReviewError("source chapter blocks are malformed")
    source_rows = []
    seen_source_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise B4EditorialReviewError("source block is malformed")
        block_id = _text(row.get("block_id"), "source block_id")
        if block_id in seen_source_ids:
            raise B4EditorialReviewError("source chapter repeats a block")
        seen_source_ids.add(block_id)
        source_rows.append(
            {
                "block_id": block_id,
                "source_text": _text(
                    row.get("clean_text")
                    if row.get("clean_text") is not None
                    else row.get("source_text"),
                    "source text",
                ),
            }
        )
    translated = translation["blocks"]
    translated_ids = [str(row["block_id"]) for row in translated]
    translated_id_set = set(translated_ids)
    selected_source_rows = [
        row for row in source_rows if row["block_id"] in translated_id_set
    ]
    if [row["block_id"] for row in selected_source_rows] != translated_ids:
        raise B4EditorialReviewError(
            "Editorial Review translation is not an ordered source subset"
        )
    for source, target in zip(selected_source_rows, translated, strict=True):
        if source["source_text"] != target["source_text"]:
            raise B4EditorialReviewError(
                "Editorial Review source text is not verbatim"
            )
    return selected_source_rows


def _verify_pack(
    *,
    translator_pack: Mapping[str, Any],
    translation: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        pack = verify_translator_pack_v1(translator_pack)
    except B4TranslatorPackError as exc:
        raise B4EditorialReviewError("Translator Pack is invalid") from exc
    if pack.get("chapter_id") != translation.get("chapter_id"):
        raise B4EditorialReviewError(
            "Translator Pack and translation chapters differ"
        )
    if (
        translation.get("translator_pack_artifact_hash")
        != pack.get("artifact_hash")
    ):
        raise B4EditorialReviewError(
            "Translator Pack and translation lineage differ"
        )
    return pack


def _verify_lint_report(
    *,
    lint_report: Mapping[str, Any],
    translation: Mapping[str, Any],
) -> dict[str, Any]:
    lint = _verify_hashed(lint_report, "lint report")
    if lint.get("schema_version") != "literary_b4_translation_lint_report_v2":
        raise B4EditorialReviewError("unsupported lint report schema")
    if lint.get("chapter_id") != translation.get("chapter_id"):
        raise B4EditorialReviewError(
            "lint report and translation chapters differ"
        )
    if (
        lint.get("source_translation_artifact_hash")
        != translation.get("artifact_hash")
    ):
        raise B4EditorialReviewError(
            "lint report and translation lineage differ"
        )
    return lint


def _verify_window_slices(
    *,
    window_slices: Sequence[Mapping[str, Any]],
    chapter_id: str,
) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for value in window_slices:
        row = _verify_hashed(value, "window slice")
        if row.get("chapter_id") != chapter_id:
            raise B4EditorialReviewError(
                "window slice and translation chapters differ"
            )
        window_id = _text(row.get("window_id"), "window slice window_id")
        if window_id in seen:
            raise B4EditorialReviewError("window slice repeats a window")
        seen.add(window_id)
        rows.append(row)
    rows.sort(key=lambda row: (int(row.get("window_order") or 0), row["window_id"]))
    return rows


def _verify_packet(value: Mapping[str, Any]) -> dict[str, Any]:
    packet = _verify_hashed(value, "Editorial Review packet")
    if packet.get("schema_version") != PACKET_SCHEMA_VERSION:
        raise B4EditorialReviewError(
            "unsupported Editorial Review packet schema"
        )
    ids = packet.get("candidate_block_ids")
    rows = packet.get("candidates")
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
        raise B4EditorialReviewError(
            "Editorial Review packet candidates are malformed"
        )
    if not isinstance(rows, list) or [
        row.get("block_id") if isinstance(row, Mapping) else None for row in rows
    ] != ids:
        raise B4EditorialReviewError(
            "Editorial Review packet candidate rows differ"
        )
    return packet


def _verify_validated_response(
    *,
    validated_response: Mapping[str, Any],
    rendered: RenderedEditorialReviewV1,
) -> dict[str, Any]:
    value = _verify_hashed(validated_response, "validated review response")
    if value.get("schema_version") != VALIDATED_RESPONSE_SCHEMA_VERSION:
        raise B4EditorialReviewError(
            "unsupported validated Editorial Review schema"
        )
    if (
        value.get("request_fingerprint") != rendered.request_fingerprint
        or value.get("review_packet_artifact_hash")
        != rendered.packet["artifact_hash"]
    ):
        raise B4EditorialReviewError(
            "validated Editorial Review response lineage differs"
        )
    return value


def _verify_review_artifact(
    *,
    review_artifact: Mapping[str, Any],
    translation: Mapping[str, Any],
) -> dict[str, Any]:
    review = _verify_hashed(review_artifact, "Editorial Review artifact")
    if review.get("schema_version") != REVIEW_ARTIFACT_SCHEMA_VERSION:
        raise B4EditorialReviewError(
            "unsupported Editorial Review artifact schema"
        )
    if (
        review.get("chapter_id") != translation.get("chapter_id")
        or review.get("source_translation_artifact_hash")
        != translation.get("artifact_hash")
    ):
        raise B4EditorialReviewError(
            "Editorial Review artifact and translation lineage differ"
        )
    return review


def _verify_approval(
    *,
    approval_artifact: Mapping[str, Any],
    translation: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
    repair_rows: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    approval = _verify_hashed(approval_artifact, "Editorial approval")
    if approval.get("schema_version") != APPROVAL_SCHEMA_VERSION:
        raise B4EditorialReviewError(
            "unsupported Editorial approval schema"
        )
    if approval.get("source_translation_artifact_hash") != translation.get(
        "artifact_hash"
    ):
        raise B4EditorialReviewError(
            "Editorial approval and translation lineage differ"
        )
    expected_hashes = [row["artifact_hash"] for row in reviews]
    if approval.get("review_artifact_hashes") != expected_hashes:
        raise B4EditorialReviewError(
            "Editorial approval review order or lineage differs"
        )
    decisions = approval.get("decisions")
    if not isinstance(decisions, list):
        raise B4EditorialReviewError(
            "Editorial approval decisions must be a list"
        )
    seen: set[tuple[str, str]] = set()
    for row in decisions:
        if not isinstance(row, Mapping):
            raise B4EditorialReviewError(
                "Editorial approval decision is malformed"
            )
        key = (
            _text(
                row.get("review_artifact_hash"),
                "approval review_artifact_hash",
            ),
            _text(row.get("block_id"), "approval block_id"),
        )
        if key in seen or key not in repair_rows:
            raise B4EditorialReviewError(
                "Editorial approval repeats or cites a non-repair proposal"
            )
        seen.add(key)
        if row.get("decision") not in {"approve", "reject"}:
            raise B4EditorialReviewError(
                "Editorial approval decision is invalid"
            )
    return approval


def build_editorial_approval_v1(
    *,
    source_translation_artifact_hash: str,
    review_artifact_hashes: Sequence[str],
    decisions: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    _hash_text(
        source_translation_artifact_hash,
        "source_translation_artifact_hash",
    )
    review_hashes = [
        _hash_text(value, "review_artifact_hash")
        for value in review_artifact_hashes
    ]
    normalized = []
    for row in decisions:
        decision = str(row.get("decision") or "")
        if decision not in {"approve", "reject"}:
            raise B4EditorialReviewError(
                "Editorial approval decision is invalid"
            )
        normalized.append(
            {
                "review_artifact_hash": _hash_text(
                    row.get("review_artifact_hash"),
                    "decision review_artifact_hash",
                ),
                "block_id": _text(row.get("block_id"), "decision block_id"),
                "decision": decision,
            }
        )
    body = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "source_translation_artifact_hash": source_translation_artifact_hash,
        "review_artifact_hashes": review_hashes,
        "decisions": normalized,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def _verify_hashed(
    value: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B4EditorialReviewError(f"{label} must be an object")
    raw = deepcopy(dict(value))
    artifact_hash = raw.pop("artifact_hash", None)
    if (
        not isinstance(artifact_hash, str)
        or canonical_hash(raw) != artifact_hash
    ):
        raise B4EditorialReviewError(f"{label} artifact hash differs")
    return {**raw, "artifact_hash": artifact_hash}


def _bounded_text(value: Any, label: str, *, maximum: int) -> str:
    text = _text(value, label)
    if len(text) > maximum:
        raise B4EditorialReviewError(f"{label} exceeds maximum length")
    return text


def _optional_bounded_text(
    value: Any,
    label: str,
    *,
    maximum: int,
) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise B4EditorialReviewError(f"{label} must be a string")
    if len(value) > maximum:
        raise B4EditorialReviewError(f"{label} exceeds maximum length")
    return value


def _hash_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise B4EditorialReviewError(f"{label} is malformed")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B4EditorialReviewError(f"{label} must be a non-empty string")
    return value


__all__ = [
    "ACTIONS",
    "APPROVAL_SCHEMA_VERSION",
    "B4EditorialReviewError",
    "EDITED_TRANSLATION_SCHEMA_VERSION",
    "ISSUE_TYPES",
    "PACKET_SCHEMA_VERSION",
    "PROMPT_ID",
    "RESPONSE_SCHEMA_VERSION",
    "REVIEW_ARTIFACT_SCHEMA_VERSION",
    "ROLE_ID",
    "RenderedEditorialReviewV1",
    "apply_approved_editorial_reviews_v1",
    "build_editorial_approval_v1",
    "build_editorial_review_artifact_v1",
    "build_editorial_review_packets_v1",
    "editorial_review_response_schema_v1",
    "render_editorial_review_request_v1",
    "validate_editorial_review_response_v1",
]
