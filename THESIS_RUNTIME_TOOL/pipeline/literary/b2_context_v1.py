"""Read-only B1 handoff and bounded context rendering for Literary B2 V1."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping, Sequence

from pipeline.literary.b2_prompts_v1 import (
    B2_FRAME_PROMPT_ID,
    B2_FRAME_SYSTEM_PROMPT,
    B2_INTERACTION_PROMPT_ID,
    B2_INTERACTION_SYSTEM_PROMPT,
    b2_frame_response_schema,
    b2_interaction_response_schema,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)


B2_PROFILE_SCHEMA_VERSION = "literary_b2_phase_a_profile_v1"
B2_INPUT_SCHEMA_VERSION = "literary_b2_real_input_v1"
B2_WINDOW_SCHEMA_VERSION = "literary_b2_window_v1"
B2_CANDIDATE_PACKET_SCHEMA_VERSION = "literary_b2_candidate_packet_v1"
B2_REQUEST_SCHEMA_VERSION = "literary_b2_rendered_request_v1"
B2_PLAN_SCHEMA_VERSION = "literary_b2_phase_a_plan_v1"
B2_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1 = frozenset(
    {
        "first_supported_block_id",
        "non_authoritative_context_statuses",
        "provenance_refs",
        "relevant_claim_transitions",
    }
)
B2_INTERACTION_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1 = (
    B2_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1
)


class B2ContextError(RuntimeError):
    pass


class B2ContextBudgetError(B2ContextError):
    pass


@dataclass(frozen=True)
class B2PhaseAProfile:
    source_path: Path
    profile_id: str
    target_active_source_tokens: int
    max_active_blocks: int
    preceding_tail_blocks: int
    frame_candidate_card_cap: int
    interaction_candidate_card_cap: int
    candidate_surface_group_cap: int
    provenance_refs_per_card: int
    uncertainty_rows_per_request: int
    frame_prompt_tokens: int
    frame_output_tokens: int
    interaction_prompt_tokens: int
    interaction_output_tokens: int
    pending_frame_context_reserve_tokens: int
    frame_calls_per_chapter: int
    interaction_calls_per_chapter: int
    exception_review_calls_per_chapter: int
    model_role_defaults: Mapping[str, str]
    safety: Mapping[str, Any]
    profile_hash: str


def load_b2_phase_a_profile(path: Path) -> B2PhaseAProfile:
    source = Path(path).resolve()
    payload = _read_object(source, "B2 profile")
    _exact_keys(
        payload,
        {
            "schema_version",
            "profile_id",
            "source_windows",
            "context_caps",
            "token_caps",
            "call_caps",
            "model_role_defaults",
            "safety",
        },
        "B2 profile",
    )
    if payload["schema_version"] != B2_PROFILE_SCHEMA_VERSION:
        raise B2ContextError("foreign B2 profile schema")
    source_windows = _object(payload["source_windows"], "source_windows")
    _exact_keys(
        source_windows,
        {
            "target_active_source_tokens",
            "max_active_blocks",
            "preceding_tail_blocks",
        },
        "source_windows",
    )
    context_caps = _object(payload["context_caps"], "context_caps")
    _exact_keys(
        context_caps,
        {
            "frame_candidate_card_cap",
            "interaction_candidate_card_cap",
            "candidate_surface_group_cap",
            "provenance_refs_per_card",
            "uncertainty_rows_per_request",
        },
        "context_caps",
    )
    token_caps = _object(payload["token_caps"], "token_caps")
    _exact_keys(
        token_caps,
        {
            "frame_prompt_tokens",
            "frame_output_tokens",
            "interaction_prompt_tokens",
            "interaction_output_tokens",
            "pending_frame_context_reserve_tokens",
        },
        "token_caps",
    )
    call_caps = _object(payload["call_caps"], "call_caps")
    _exact_keys(
        call_caps,
        {
            "frame_calls_per_chapter",
            "interaction_calls_per_chapter",
            "exception_review_calls_per_chapter",
        },
        "call_caps",
    )
    model_roles = _object(payload["model_role_defaults"], "model_role_defaults")
    _exact_keys(
        model_roles,
        {
            "frame_reference",
            "interaction_scale_candidate",
            "interaction_reference",
            "exception_review",
        },
        "model_role_defaults",
    )
    if any(not isinstance(value, str) or not value for value in model_roles.values()):
        raise B2ContextError("B2 model role default is empty")
    safety = _object(payload["safety"], "safety")
    expected_safety = {
        "semantic_ambiguity_action": "persist_pending_and_continue",
        "integrity_failure_action": "pause_before_next_api_call",
        "candidate_overflow_action": "split_then_pause_if_single_block",
        "gold_in_runtime_request_allowed": False,
        "production_publish_enabled": False,
    }
    if safety != expected_safety:
        raise B2ContextError("B2 safety contract was weakened or changed")
    return B2PhaseAProfile(
        source_path=source,
        profile_id=_required_string(payload["profile_id"], "profile_id"),
        target_active_source_tokens=_bounded_int(
            source_windows["target_active_source_tokens"],
            "target_active_source_tokens",
            256,
            20_000,
        ),
        max_active_blocks=_bounded_int(
            source_windows["max_active_blocks"], "max_active_blocks", 1, 128
        ),
        preceding_tail_blocks=_bounded_int(
            source_windows["preceding_tail_blocks"],
            "preceding_tail_blocks",
            0,
            16,
        ),
        frame_candidate_card_cap=_bounded_int(
            context_caps["frame_candidate_card_cap"],
            "frame_candidate_card_cap",
            1,
            256,
        ),
        interaction_candidate_card_cap=_bounded_int(
            context_caps["interaction_candidate_card_cap"],
            "interaction_candidate_card_cap",
            1,
            256,
        ),
        candidate_surface_group_cap=_bounded_int(
            context_caps["candidate_surface_group_cap"],
            "candidate_surface_group_cap",
            1,
            256,
        ),
        provenance_refs_per_card=_bounded_int(
            context_caps["provenance_refs_per_card"],
            "provenance_refs_per_card",
            0,
            32,
        ),
        uncertainty_rows_per_request=_bounded_int(
            context_caps["uncertainty_rows_per_request"],
            "uncertainty_rows_per_request",
            0,
            128,
        ),
        frame_prompt_tokens=_bounded_int(
            token_caps["frame_prompt_tokens"], "frame_prompt_tokens", 1_000, 100_000
        ),
        frame_output_tokens=_bounded_int(
            token_caps["frame_output_tokens"], "frame_output_tokens", 256, 32_000
        ),
        interaction_prompt_tokens=_bounded_int(
            token_caps["interaction_prompt_tokens"],
            "interaction_prompt_tokens",
            1_000,
            100_000,
        ),
        interaction_output_tokens=_bounded_int(
            token_caps["interaction_output_tokens"],
            "interaction_output_tokens",
            256,
            32_000,
        ),
        pending_frame_context_reserve_tokens=_bounded_int(
            token_caps["pending_frame_context_reserve_tokens"],
            "pending_frame_context_reserve_tokens",
            0,
            10_000,
        ),
        frame_calls_per_chapter=_bounded_int(
            call_caps["frame_calls_per_chapter"],
            "frame_calls_per_chapter",
            1,
            1,
        ),
        interaction_calls_per_chapter=_bounded_int(
            call_caps["interaction_calls_per_chapter"],
            "interaction_calls_per_chapter",
            1,
            16,
        ),
        exception_review_calls_per_chapter=_bounded_int(
            call_caps["exception_review_calls_per_chapter"],
            "exception_review_calls_per_chapter",
            0,
            8,
        ),
        model_role_defaults=dict(model_roles),
        safety=dict(safety),
        profile_hash=canonical_hash(payload),
    )


def load_real_b1_run_input_v1(
    run_root: Path, *, current_git_head: str | None = None
) -> dict[str, Any]:
    """Load a verified completed-prefix B1/Auditor run without mutating it."""

    root = Path(run_root).resolve()
    if not root.is_dir():
        raise B2ContextError(f"B1 run root is absent: {root}")
    plan = _read_object(root / "run_plan.json", "run plan")
    summary = _read_object(root / "run_summary.json", "run summary")
    _verify_embedded_hash(plan, "plan_hash", "run plan")
    _verify_embedded_hash(summary, "summary_hash", "run summary")
    if summary.get("status") not in {"stopped", "complete"}:
        raise B2ContextError("B1 run is not at a completed chapter boundary")
    if summary.get("production_publish_performed") is not False:
        raise B2ContextError("B1 run claims production publication")
    if (summary.get("b2") or {}).get("enabled") is not False:
        raise B2ContextError("source B1 run unexpectedly contains B2")
    if summary.get("plan_hash") != plan.get("plan_hash"):
        raise B2ContextError("run summary points to a different plan")

    document_path = Path(_required_string(plan.get("document_path"), "document_path"))
    if not document_path.is_file():
        raise B2ContextError("sealed source document is absent")
    if file_sha256(document_path) != str(plan.get("document_sha256") or ""):
        raise B2ContextError("sealed source document hash changed")
    document = _read_object(document_path, "source document")
    document_chapters = _document_chapters(document)
    sealed_ids = [
        _required_string(value, "ordered_chapter_id")
        for value in plan.get("ordered_chapter_ids") or []
    ]
    if not sealed_ids or sealed_ids != [
        row["chapter_id"] for row in document_chapters[: len(sealed_ids)]
    ]:
        raise B2ContextError("B1 run is not a contiguous document prefix")
    completed_ids = [
        _required_string(value, "completed_chapter_id")
        for value in summary.get("completed_chapter_ids") or []
    ]
    if (
        not completed_ids
        or completed_ids != sealed_ids[: len(completed_ids)]
        or (
            summary.get("status") == "complete"
            and completed_ids != sealed_ids
        )
    ):
        raise B2ContextError(
            "B1 summary does not exact-cover a completed sealed prefix"
        )

    report_rows = summary.get("chapter_reports")
    if not isinstance(report_rows, list) or len(report_rows) != len(completed_ids):
        raise B2ContextError("B1 chapter report index is incomplete")
    chapters: list[dict[str, Any]] = []
    for ordinal, (chapter_id, report_row) in enumerate(
        zip(completed_ids, report_rows), 1
    ):
        if not isinstance(report_row, Mapping) or report_row.get("chapter_id") != chapter_id:
            raise B2ContextError("B1 chapter report order drifted")
        report_path = _contained_path(root, report_row.get("path"), "chapter report")
        report = _read_object(report_path, "chapter report")
        _verify_embedded_hash(report, "report_hash", "chapter report")
        if report.get("report_hash") != report_row.get("report_hash"):
            raise B2ContextError("chapter report index hash is stale")
        if report.get("b2_enabled") is not False or report.get("b2_ready") is not False:
            raise B2ContextError("source chapter report claims B2 readiness")
        prefix_path = report_path.parent / "final_prefix.json"
        prefix = verify_chapter_prefix_prior_bundle_v1(
            _read_object(prefix_path, "chapter prefix"), document=document
        )
        if prefix.get("coverage_through_chapter_id") != chapter_id:
            raise B2ContextError("chapter prefix coverage is stale")
        if prefix.get("prefix_bundle_hash") != report.get("prefix_bundle_hash"):
            raise B2ContextError("chapter report points to a different prefix")
        chapters.append(
            {
                "chapter_id": chapter_id,
                "chapter_ordinal": ordinal,
                "chapter": deepcopy(document_chapters[ordinal - 1]),
                "chapter_report_path": str(report_path),
                "chapter_report_hash": report["report_hash"],
                "prefix_path": str(prefix_path),
                "prefix_bundle": prefix,
                "prefix_bundle_hash": prefix["prefix_bundle_hash"],
            }
        )

    source_heads = _discover_source_git_heads(root)
    source_git_head = source_heads[0] if len(source_heads) == 1 else None
    blockers: list[str] = []
    if len(source_heads) != 1:
        blockers.append("source_run_git_head_not_unique")
    normalized_current = str(current_git_head or "").strip() or None
    if normalized_current is None:
        blockers.append("current_git_head_not_declared")
    elif source_git_head != normalized_current:
        blockers.append("source_run_head_differs_from_current_head")
    body = {
        "schema_version": B2_INPUT_SCHEMA_VERSION,
        "source_run_root": str(root),
        "source_plan_hash": plan["plan_hash"],
        "source_summary_hash": summary["summary_hash"],
        "source_document_path": str(document_path.resolve()),
        "source_document_sha256": plan["document_sha256"],
        "source_run_git_heads": source_heads,
        "source_run_git_head": source_git_head,
        "current_git_head": normalized_current,
        "certification_eligible": not blockers,
        "certification_blockers": blockers,
        "ordered_chapter_ids": completed_ids,
        "sealed_chapter_ids": sealed_ids,
        "chapters": chapters,
        "historical_artifact_mutated": False,
    }
    return {**body, "input_hash": canonical_hash(body)}


def build_b2_windows_v1(
    chapter: Mapping[str, Any], *, profile: B2PhaseAProfile
) -> list[dict[str, Any]]:
    all_blocks = _chapter_blocks(chapter)
    active = [row for row in all_blocks if not _is_heading(row)]
    if not active:
        return []
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for block in active:
        block_tokens = _estimated_text_tokens(_block_text(block))
        if current and (
            current_tokens + block_tokens > profile.target_active_source_tokens
            or len(current) >= profile.max_active_blocks
        ):
            groups.append(current)
            current = []
            current_tokens = 0
        if block_tokens > profile.target_active_source_tokens:
            groups.append([block])
            continue
        current.append(block)
        current_tokens += block_tokens
    if current:
        groups.append(current)
    result = [
        _window_from_blocks(
            chapter=chapter,
            all_blocks=all_blocks,
            active_blocks=rows,
            ordinal=index,
            preceding_tail_blocks=profile.preceding_tail_blocks,
        )
        for index, rows in enumerate(groups, 1)
    ]
    covered = [block_id for row in result for block_id in row["active_block_ids"]]
    expected = [str(row["block_id"]) for row in active]
    if covered != expected or len(covered) != len(set(covered)):
        raise B2ContextError("B2 windows do not exact-cover active source blocks")
    if len(result) > profile.interaction_calls_per_chapter:
        raise B2ContextBudgetError("initial B2 window count exceeds per-chapter call cap")
    return result


def build_candidate_packet_v1(
    *,
    chapter_id: str,
    active_blocks: Sequence[Mapping[str, Any]],
    tail_blocks: Sequence[Mapping[str, Any]],
    prefix_bundle: Mapping[str, Any],
    candidate_card_cap: int,
    profile: B2PhaseAProfile,
    supplemental_candidate_sources: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    cards = list(prefix_bundle.get("b0_context_cards") or []) + list(
        prefix_bundle.get("candidate_only_context_cards") or []
    )
    cards_by_id: dict[str, dict[str, Any]] = {}
    for raw_card in cards:
        card = _object(raw_card, "prefix candidate card")
        card_id = _required_string(card.get("prior_card_id"), "prior_card_id")
        if card_id in cards_by_id:
            raise B2ContextError("prefix candidate card id is repeated")
        cards_by_id[card_id] = card
    block_rows = [
        ("active", row) for row in active_blocks
    ] + [("preceding_tail", row) for row in tail_blocks]
    active_ids = {str(row["block_id"]) for row in active_blocks}
    matched_by_card: dict[str, dict[str, Any]] = {}
    surface_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for card_id, card in cards_by_id.items():
        stable_surfaces = _stable_card_surfaces(card)
        for source_scope, block in block_rows:
            block_id = _required_string(block.get("block_id"), "block_id")
            text = _block_text(block)
            for stable_surface in stable_surfaces:
                for match_kind, matched_surface in _surface_matches(
                    text, stable_surface
                ):
                    group_key = (_normalized_surface(matched_surface), source_scope)
                    group = surface_groups.setdefault(
                        group_key,
                        {
                            "source_surface": matched_surface,
                            "source_scope": source_scope,
                            "block_ids": [],
                            "match_kinds": [],
                            "candidate_card_ids": [],
                        },
                    )
                    _append_unique(group["block_ids"], block_id)
                    _append_unique(group["match_kinds"], match_kind)
                    _append_unique(group["candidate_card_ids"], card_id)
                    matched_by_card[card_id] = card
        support_ids = {
            str(row.get("block_id"))
            for row in card.get("provenance_refs") or []
            if isinstance(row, Mapping)
        }
        for block_id in sorted(active_ids.intersection(support_ids)):
            key = (f"support:{card_id}", "active")
            group = surface_groups.setdefault(
                key,
                {
                    "source_surface": None,
                    "source_scope": "active",
                    "block_ids": [],
                    "match_kinds": [],
                    "candidate_card_ids": [],
                },
            )
            _append_unique(group["block_ids"], block_id)
            _append_unique(group["match_kinds"], "support_block")
            _append_unique(group["candidate_card_ids"], card_id)
            matched_by_card[card_id] = card

    context_candidate_sources: list[dict[str, Any]] = []
    allowed_source_kinds = {
        "prior_frame_narrator_candidate",
        "current_frame_narrator_candidate",
    }
    for raw_source in supplemental_candidate_sources:
        source = _object(raw_source, "supplemental candidate source")
        _exact_keys(
            source,
            {
                "candidate_card_id",
                "source_kind",
                "source_chapter_id",
                "source_artifact_hash",
                "source_frame_segment_ids",
            },
            "supplemental candidate source",
        )
        card_id = _required_string(
            source.get("candidate_card_id"), "supplemental candidate card id"
        )
        source_kind = _required_string(
            source.get("source_kind"), "supplemental candidate source kind"
        )
        if source_kind not in allowed_source_kinds:
            raise B2ContextError("supplemental candidate source kind is unsupported")
        source_chapter_id = _required_string(
            source.get("source_chapter_id"),
            "supplemental candidate source chapter id",
        )
        source_artifact_hash = _required_string(
            source.get("source_artifact_hash"),
            "supplemental candidate source artifact hash",
        )
        segment_ids = source.get("source_frame_segment_ids")
        if not isinstance(segment_ids, list) or not segment_ids:
            raise B2ContextError(
                "supplemental candidate source segment ids must be non-empty"
            )
        normalized_segment_ids: list[str] = []
        for value in segment_ids:
            segment_id = _required_string(value, "source frame segment id")
            _append_unique(normalized_segment_ids, segment_id)
        card = cards_by_id.get(card_id)
        if card is None:
            raise B2ContextError(
                "supplemental candidate is absent from the current prefix"
            )
        rendered_source = {
            "candidate_card_id": card_id,
            "source_kind": source_kind,
            "source_chapter_id": source_chapter_id,
            "source_artifact_hash": source_artifact_hash,
            "source_frame_segment_ids": sorted(normalized_segment_ids),
        }
        if rendered_source not in context_candidate_sources:
            context_candidate_sources.append(rendered_source)
        matched_by_card[card_id] = card

    selected_ids = sorted(matched_by_card)
    groups = sorted(
        surface_groups.values(),
        key=lambda row: (
            0 if row["source_scope"] == "active" else 1,
            str(row["source_surface"] or ""),
            tuple(row["block_ids"]),
        ),
    )
    overflow_reasons: list[str] = []
    if len(selected_ids) > candidate_card_cap:
        overflow_reasons.append("candidate_card_cap")
    if len(groups) > profile.candidate_surface_group_cap:
        overflow_reasons.append("candidate_surface_group_cap")
    selected_set = set(selected_ids)
    uncertainties = [
        deepcopy(row)
        for row in prefix_bundle.get("prefix_identity_uncertainties") or []
        if selected_set.intersection(row.get("prior_card_ids") or [])
    ]
    if len(uncertainties) > profile.uncertainty_rows_per_request:
        overflow_reasons.append("uncertainty_rows_per_request")
    rendered_cards = [
        _render_candidate_card(
            matched_by_card[card_id],
            provenance_cap=profile.provenance_refs_per_card,
        )
        for card_id in selected_ids
    ]
    body = {
        "schema_version": B2_CANDIDATE_PACKET_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "prefix_bundle_hash": prefix_bundle.get("prefix_bundle_hash"),
        "active_block_ids": [str(row["block_id"]) for row in active_blocks],
        "preceding_tail_block_ids": [str(row["block_id"]) for row in tail_blocks],
        "surface_groups": groups,
        "candidate_cards": rendered_cards,
        "identity_uncertainties": uncertainties,
        "claim_transition_coverage": "not_available_in_prefix_v1",
        "overflow": bool(overflow_reasons),
        "overflow_reasons": overflow_reasons,
    }
    if context_candidate_sources:
        body["context_candidate_sources"] = sorted(
            context_candidate_sources,
            key=lambda row: (
                row["source_kind"],
                row["source_chapter_id"],
                row["candidate_card_id"],
            ),
        )
    return {**body, "packet_hash": canonical_hash(body)}


def project_b2_candidate_packet_for_model_v1(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove audit-only lifecycle detail without changing decision inputs."""

    source_body = deepcopy(dict(packet))
    source_hash = _required_string(
        source_body.pop("packet_hash", None), "candidate packet hash"
    )
    if canonical_hash(source_body) != source_hash:
        raise B2ContextError("candidate packet hash mismatch before model projection")
    raw_cards = source_body.get("candidate_cards")
    if not isinstance(raw_cards, list):
        raise B2ContextError("candidate packet cards must be a list")
    projected_cards: list[dict[str, Any]] = []
    for raw_card in raw_cards:
        card = _object(raw_card, "candidate packet card")
        projected = deepcopy(dict(card))
        for field in B2_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1:
            projected.pop(field, None)
        _compact_candidate_uncertainty_flags_v1(projected)
        projected_cards.append(projected)
    source_body["candidate_cards"] = projected_cards
    return {**source_body, "packet_hash": canonical_hash(source_body)}


def project_b2_interaction_candidate_packet_for_model_v1(
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Project active-window candidates without audit-only lifecycle detail."""

    source_body = deepcopy(dict(packet))
    source_hash = _required_string(
        source_body.pop("packet_hash", None), "candidate packet hash"
    )
    if canonical_hash(source_body) != source_hash:
        raise B2ContextError("candidate packet hash mismatch before model projection")
    if source_body.get("preceding_tail_block_ids"):
        raise B2ContextError(
            "interaction candidate projection includes preceding-tail retrieval"
        )
    raw_cards = source_body.get("candidate_cards")
    if not isinstance(raw_cards, list):
        raise B2ContextError("candidate packet cards must be a list")
    projected_cards: list[dict[str, Any]] = []
    for raw_card in raw_cards:
        card = _object(raw_card, "candidate packet card")
        projected = deepcopy(dict(card))
        for field in B2_INTERACTION_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1:
            projected.pop(field, None)
        _compact_candidate_uncertainty_flags_v1(projected)
        projected_cards.append(projected)
    source_body["candidate_cards"] = projected_cards
    return {**source_body, "packet_hash": canonical_hash(source_body)}


def _compact_candidate_uncertainty_flags_v1(card: dict[str, Any]) -> None:
    raw_flags = card.get("uncertainty_flags")
    if not isinstance(raw_flags, list):
        raise B2ContextError("candidate uncertainty flags must be a list")
    compact_flags: list[dict[str, Any]] = []
    for raw_flag in raw_flags:
        flag = _object(raw_flag, "candidate uncertainty flag")
        compact_flags.append(
            {
                "disputed_field": flag.get("disputed_field"),
                "status": flag.get("status"),
                "pending_reason_codes": deepcopy(
                    list(flag.get("pending_reason_codes") or [])
                ),
            }
        )
    if compact_flags:
        card["uncertainty_flags"] = compact_flags
    else:
        card.pop("uncertainty_flags", None)


def render_b2_frame_request_v1(
    *,
    chapter: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    profile: B2PhaseAProfile,
    supplemental_candidate_sources: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    blocks = [row for row in _chapter_blocks(chapter) if not _is_heading(row)]
    packet = build_candidate_packet_v1(
        chapter_id=chapter_id,
        active_blocks=blocks,
        tail_blocks=[],
        prefix_bundle=prefix_bundle,
        candidate_card_cap=profile.frame_candidate_card_cap,
        profile=profile,
        supplemental_candidate_sources=supplemental_candidate_sources,
    )
    if packet["overflow"]:
        raise B2ContextBudgetError(
            f"B2 frame candidate context overflow: {packet['overflow_reasons']}"
        )
    user_payload = {
        "request_kind": "chapter_frame",
        "chapter_id": chapter_id,
        "chapter_blocks": [_source_block_view(row) for row in blocks],
        "candidate_packets": packet,
    }
    messages = [
        {"role": "system", "content": B2_FRAME_SYSTEM_PROMPT},
        {"role": "user", "content": canonical_json(user_payload)},
    ]
    response_schema = b2_frame_response_schema()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.frame_output_tokens,
    )
    if reserve.prompt_token_reserve > profile.frame_prompt_tokens:
        raise B2ContextBudgetError("B2 frame prompt exceeds configured cap")
    return _rendered_request(
        request_kind="chapter_frame",
        prompt_id=B2_FRAME_PROMPT_ID,
        prompt_text=B2_FRAME_SYSTEM_PROMPT,
        chapter_id=chapter_id,
        window_id=None,
        messages=messages,
        response_schema=response_schema,
        reserve=reserve.to_payload(),
        configured_prompt_cap=profile.frame_prompt_tokens,
        dependency_status="ready",
        api_eligible=False,
        api_ineligible_reasons=["phase_a_zero_api"],
        context_hashes={"candidate_packet_hash": packet["packet_hash"]},
    )


def render_b2_interaction_request_v1(
    *,
    window: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    profile: B2PhaseAProfile,
    frame_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    chapter_id = _required_string(window.get("chapter_id"), "window chapter_id")
    window_id = _required_string(window.get("window_id"), "window_id")
    active_blocks = list(window.get("active_blocks") or [])
    tail_blocks = list(window.get("preceding_tail") or [])
    supplemental_sources = _frame_context_candidate_sources_v1(
        frame_context=frame_context,
        chapter_id=chapter_id,
    )
    packet = build_candidate_packet_v1(
        chapter_id=chapter_id,
        active_blocks=active_blocks,
        tail_blocks=tail_blocks,
        prefix_bundle=prefix_bundle,
        candidate_card_cap=profile.interaction_candidate_card_cap,
        profile=profile,
        supplemental_candidate_sources=supplemental_sources,
    )
    if packet["overflow"]:
        raise B2ContextBudgetError(
            f"B2 interaction candidate context overflow: {packet['overflow_reasons']}"
        )
    dependency_status = "ready" if frame_context is not None else "pending_b2a"
    user_payload = {
        "request_kind": "window_interaction",
        "chapter_id": chapter_id,
        "window_id": window_id,
        "active_blocks": [_source_block_view(row) for row in active_blocks],
        "preceding_tail": [_source_block_view(row) for row in tail_blocks],
        "frame_context_status": dependency_status,
        "frame_context": deepcopy(frame_context) if frame_context is not None else None,
        "candidate_packets": packet,
        "prior_relation_states": [],
    }
    messages = [
        {"role": "system", "content": B2_INTERACTION_SYSTEM_PROMPT},
        {"role": "user", "content": canonical_json(user_payload)},
    ]
    response_schema = b2_interaction_response_schema()
    reserve = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=response_schema,
        output_token_cap=profile.interaction_output_tokens,
    ).to_payload()
    conservative_prompt_reserve = reserve["prompt_token_reserve"]
    if frame_context is None:
        conservative_prompt_reserve += profile.pending_frame_context_reserve_tokens
    reserve["dependency_context_reserve_tokens"] = (
        profile.pending_frame_context_reserve_tokens if frame_context is None else 0
    )
    reserve["conservative_prompt_token_reserve"] = conservative_prompt_reserve
    reserve["conservative_total_token_reserve"] = (
        conservative_prompt_reserve + profile.interaction_output_tokens
    )
    if conservative_prompt_reserve > profile.interaction_prompt_tokens:
        raise B2ContextBudgetError("B2 interaction prompt exceeds configured cap")
    reasons = ["phase_a_zero_api"]
    if frame_context is None:
        reasons.append("b2a_dependency_not_available")
    return _rendered_request(
        request_kind="window_interaction",
        prompt_id=B2_INTERACTION_PROMPT_ID,
        prompt_text=B2_INTERACTION_SYSTEM_PROMPT,
        chapter_id=chapter_id,
        window_id=window_id,
        messages=messages,
        response_schema=response_schema,
        reserve=reserve,
        configured_prompt_cap=profile.interaction_prompt_tokens,
        dependency_status=dependency_status,
        api_eligible=False,
        api_ineligible_reasons=reasons,
        context_hashes={
            "candidate_packet_hash": packet["packet_hash"],
            "window_hash": window["window_hash"],
        },
    )


def _frame_context_candidate_sources_v1(
    *,
    frame_context: Mapping[str, Any] | None,
    chapter_id: str,
) -> list[dict[str, Any]]:
    if frame_context is None:
        return []
    segments = frame_context.get("applicable_segments")
    if segments is None:
        return []
    if not isinstance(segments, list):
        raise B2ContextError("applicable frame segments must be an array")
    artifact_hash = _required_string(
        frame_context.get("frame_artifact_hash"), "frame context artifact hash"
    )
    by_card: dict[str, list[str]] = {}
    for raw_segment in segments:
        segment = _object(raw_segment, "applicable frame segment")
        segment_id = _required_string(
            segment.get("frame_segment_id"), "frame segment id"
        )
        card_ids = segment.get("candidate_card_ids") or []
        if not isinstance(card_ids, list):
            raise B2ContextError("frame candidate card ids must be an array")
        for value in card_ids:
            card_id = _required_string(value, "frame candidate card id")
            by_card.setdefault(card_id, [])
            _append_unique(by_card[card_id], segment_id)
    return [
        {
            "candidate_card_id": card_id,
            "source_kind": "current_frame_narrator_candidate",
            "source_chapter_id": chapter_id,
            "source_artifact_hash": artifact_hash,
            "source_frame_segment_ids": sorted(segment_ids),
        }
        for card_id, segment_ids in sorted(by_card.items())
    ]


def split_b2_window_v1(
    *, window: Mapping[str, Any], chapter: Mapping[str, Any], profile: B2PhaseAProfile
) -> list[dict[str, Any]]:
    active = list(window.get("active_blocks") or [])
    if len(active) <= 1:
        raise B2ContextBudgetError("one-block B2 window cannot be split further")
    midpoint = len(active) // 2
    all_blocks = _chapter_blocks(chapter)
    parent_hash = _required_string(window.get("window_hash"), "parent window hash")[:10]
    return [
        _window_from_blocks(
            chapter=chapter,
            all_blocks=all_blocks,
            active_blocks=rows,
            ordinal=index,
            preceding_tail_blocks=profile.preceding_tail_blocks,
            window_suffix=f"{parent_hash}_split{index}",
        )
        for index, rows in enumerate((active[:midpoint], active[midpoint:]), 1)
    ]


def _rendered_request(
    *,
    request_kind: str,
    prompt_id: str,
    prompt_text: str,
    chapter_id: str,
    window_id: str | None,
    messages: Sequence[Mapping[str, Any]],
    response_schema: Mapping[str, Any],
    reserve: Mapping[str, Any],
    configured_prompt_cap: int,
    dependency_status: str,
    api_eligible: bool,
    api_ineligible_reasons: Sequence[str],
    context_hashes: Mapping[str, str],
) -> dict[str, Any]:
    body = {
        "schema_version": B2_REQUEST_SCHEMA_VERSION,
        "request_kind": request_kind,
        "prompt_id": prompt_id,
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "chapter_id": chapter_id,
        "window_id": window_id,
        "messages": deepcopy(list(messages)),
        "response_schema": deepcopy(dict(response_schema)),
        "response_schema_hash": canonical_hash(response_schema),
        "token_reserve": deepcopy(dict(reserve)),
        "configured_prompt_cap": configured_prompt_cap,
        "dependency_status": dependency_status,
        "api_eligible": api_eligible,
        "api_ineligible_reasons": list(api_ineligible_reasons),
        "context_hashes": dict(context_hashes),
        "production_publish_performed": False,
    }
    return {**body, "request_fingerprint": canonical_hash(body)}


def _render_candidate_card(
    card: Mapping[str, Any], *, provenance_cap: int
) -> dict[str, Any]:
    disputed = [
        deepcopy(row)
        for row in card.get("disputed_claims") or []
        if isinstance(row, Mapping)
    ]
    effective = deepcopy(dict(card.get("effective_claims") or {}))
    for row in disputed:
        field = str(row.get("disputed_field") or "")
        if field in effective:
            effective.pop(field, None)
    uncertainty_flags = [
        {
            "disputed_field": row.get("disputed_field"),
            "status": row.get("status"),
            "pending_reason_codes": list(row.get("pending_reason_codes") or []),
            "next_review_trigger": row.get("next_review_trigger"),
            "hearing_count": row.get("hearing_count"),
        }
        for row in disputed
    ]
    result = {
        "candidate_card_id": _required_string(card.get("prior_card_id"), "prior_card_id"),
        "canonical_surface": _required_string(
            card.get("canonical_surface"), "canonical_surface"
        ),
        "stable_surfaces": _stable_card_surfaces(card),
        "authority_scope": _required_string(card.get("authority_scope"), "authority_scope"),
        "effective_claims_as_of": effective,
        "relevant_claim_transitions": [],
        "uncertainty_flags": uncertainty_flags,
        "first_supported_block_id": card.get("first_supported_block_id"),
        "provenance_refs": deepcopy(
            list(card.get("provenance_refs") or [])[:provenance_cap]
        ),
    }
    provisional = card.get("non_authoritative_context_claims")
    if isinstance(provisional, Mapping) and provisional:
        result["non_authoritative_context_claims"] = deepcopy(dict(provisional))
        statuses = card.get("non_authoritative_context_statuses")
        if isinstance(statuses, Mapping) and statuses:
            result["non_authoritative_context_statuses"] = deepcopy(dict(statuses))
    return result


def _surface_matches(text: str, stable_surface: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for matched in _literal_matches(text, stable_surface):
        matches.append(("stable_surface", matched))
    tokens = re.findall(r"[^\W_]+", stable_surface, flags=re.UNICODE)
    if len(tokens) >= 2:
        terminal = tokens[-1]
        if len(terminal) >= 3 and terminal[0].isupper():
            for matched in _literal_matches(text, terminal):
                if ("stable_surface", matched) not in matches:
                    matches.append(("terminal_token", matched))
    return matches


def _literal_matches(text: str, surface: str) -> list[str]:
    if not surface:
        return []
    pattern = re.compile(rf"(?<!\w){re.escape(surface)}(?!\w)", re.IGNORECASE)
    return [match.group(0) for match in pattern.finditer(text)]


def _stable_card_surfaces(card: Mapping[str, Any]) -> list[str]:
    values = [card.get("canonical_surface"), *(card.get("stable_surfaces") or [])]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = _normalized_surface(value)
        if normalized not in seen:
            seen.add(normalized)
            result.append(value)
    return result


def _normalized_surface(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _window_from_blocks(
    *,
    chapter: Mapping[str, Any],
    all_blocks: Sequence[Mapping[str, Any]],
    active_blocks: Sequence[Mapping[str, Any]],
    ordinal: int,
    preceding_tail_blocks: int,
    window_suffix: str | None = None,
) -> dict[str, Any]:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    order_by_id = {
        _required_string(row.get("block_id"), "block_id"): index
        for index, row in enumerate(all_blocks)
    }
    active = [deepcopy(dict(row)) for row in active_blocks]
    first_id = _required_string(active[0].get("block_id"), "active first block")
    first_index = order_by_id[first_id]
    tail_candidates = [
        row for row in all_blocks[:first_index] if not _is_heading(row)
    ]
    selected_tail = (
        []
        if preceding_tail_blocks == 0
        else tail_candidates[-preceding_tail_blocks:]
    )
    tail = [deepcopy(dict(row)) for row in selected_tail]
    suffix = f"_{window_suffix}" if window_suffix else ""
    body = {
        "schema_version": B2_WINDOW_SCHEMA_VERSION,
        "window_id": f"b2w1_{chapter_id}_{ordinal:02d}{suffix}",
        "chapter_id": chapter_id,
        "active_block_ids": [str(row["block_id"]) for row in active],
        "preceding_tail_block_ids": [str(row["block_id"]) for row in tail],
        "active_blocks": active,
        "preceding_tail": tail,
        "estimated_active_source_tokens": sum(
            _estimated_text_tokens(_block_text(row)) for row in active
        ),
    }
    return {**body, "window_hash": canonical_hash(body)}


def _source_block_view(block: Mapping[str, Any]) -> dict[str, str]:
    return {
        "block_id": _required_string(block.get("block_id"), "block_id"),
        "block_type": str(block.get("block_type") or "paragraph"),
        "text": _block_text(block),
    }


def _chapter_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocks = chapter.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise B2ContextError("chapter has no source blocks")
    result = [deepcopy(_object(row, "source block")) for row in blocks]
    ids = [_required_string(row.get("block_id"), "block_id") for row in result]
    if len(ids) != len(set(ids)):
        raise B2ContextError("chapter repeats a block id")
    return result


def _document_chapters(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise B2ContextError("source document has no chapters")
    result = [deepcopy(_object(row, "source chapter")) for row in chapters]
    ids = [_required_string(row.get("chapter_id"), "chapter_id") for row in result]
    if len(ids) != len(set(ids)):
        raise B2ContextError("source document repeats a chapter id")
    return result


def _is_heading(block: Mapping[str, Any]) -> bool:
    return str(block.get("block_type") or "").casefold() in {
        "heading",
        "chapter_heading",
    }


def _block_text(block: Mapping[str, Any]) -> str:
    value = block.get("clean_text")
    if not isinstance(value, str) or not value:
        value = block.get("source_text")
    if not isinstance(value, str):
        raise B2ContextError("source block text is not a string")
    return value


def _estimated_text_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _discover_source_git_heads(root: Path) -> list[str]:
    heads: set[str] = set()
    for path in root.glob("stages/*/live/run_envelope_*.json"):
        try:
            payload = _read_object(path, "run envelope")
        except B2ContextError:
            continue
        value = payload.get("git_head")
        if isinstance(value, str) and value.strip():
            heads.add(value.strip())
    return sorted(heads)


def _verify_embedded_hash(payload: Mapping[str, Any], field: str, label: str) -> None:
    body = deepcopy(dict(payload))
    observed = _required_string(body.pop(field, None), field)
    if canonical_hash(body) != observed:
        raise B2ContextError(f"{label} {field} mismatch")


def _contained_path(root: Path, value: Any, label: str) -> Path:
    relative = Path(_required_string(value, label))
    if relative.is_absolute():
        raise B2ContextError(f"{label} must be relative to run root")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise B2ContextError(f"{label} escapes run root") from exc
    if not path.is_file():
        raise B2ContextError(f"{label} is absent: {path}")
    return path


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise B2ContextError(f"cannot read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise B2ContextError(f"{label} must be an object")
    return payload


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B2ContextError(f"{label} must be an object")
    return dict(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise B2ContextError(f"{label} has a foreign key set")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2ContextError(f"{label} must be a non-empty string")
    return value


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise B2ContextError(f"{label} is outside its closed bounds")
    return value


def _append_unique(target: list[Any], value: Any) -> None:
    if value not in target:
        target.append(value)


__all__ = [
    "B2ContextBudgetError",
    "B2ContextError",
    "B2_INTERACTION_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1",
    "B2_MODEL_OMITTED_CANDIDATE_CARD_FIELDS_V1",
    "B2PhaseAProfile",
    "build_b2_windows_v1",
    "build_candidate_packet_v1",
    "load_b2_phase_a_profile",
    "load_real_b1_run_input_v1",
    "project_b2_candidate_packet_for_model_v1",
    "project_b2_interaction_candidate_packet_for_model_v1",
    "render_b2_frame_request_v1",
    "render_b2_interaction_request_v1",
    "split_b2_window_v1",
]
