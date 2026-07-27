"""Zero-API planner and immutable dry-render writer for Literary B2 V3."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.b2_context_v1 import (
    B2ContextBudgetError,
    B2ContextError,
    B2PhaseAProfile,
    build_b2_windows_v1,
    split_b2_window_v1,
)
from pipeline.literary.b1_registry_to_b2_input_v1 import load_b2_source_input_v1
from pipeline.literary.b2_context_v3 import (
    render_b2_frame_request_v2,
    render_b2_interaction_request_v3,
)
from pipeline.literary.b2_phase_a_v1 import (
    _is_within,
    _mapping,
    _path_component,
    _request_reserves,
    _required_string,
    _tree_hash,
    _write_new_json,
)
from pipeline.literary.checkpoint import canonical_hash


B2_SLIM_PLAN_SCHEMA_VERSION = "literary_b2_slim_phase_a_plan_v1"
B2_SLIM_PHASE_A_REPORT_SCHEMA_VERSION = "literary_b2_slim_phase_a_report_v1"


def build_b2_slim_phase_a_bundle_v1(
    *, real_input: Mapping[str, Any], profile: B2PhaseAProfile
) -> dict[str, Any]:
    chapters = real_input.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise B2ContextError("B2 slim real input has no chapters")
    chapter_plans: list[dict[str, Any]] = []
    request_rows: list[dict[str, Any]] = []
    total_prompt_reserve = 0
    total_output_reserve = 0
    total_calls = 0

    for chapter_row in chapters:
        if not isinstance(chapter_row, Mapping):
            raise B2ContextError("B2 slim chapter row must be an object")
        chapter_id = _required_string(chapter_row.get("chapter_id"), "chapter_id")
        chapter = _mapping(chapter_row.get("chapter"), "chapter")
        prefix = _mapping(chapter_row.get("prefix_bundle"), "prefix_bundle")
        frame_request = render_b2_frame_request_v2(
            chapter=chapter,
            prefix_bundle=prefix,
            profile=profile,
        )
        windows = _bounded_interaction_windows_v3(
            chapter=chapter,
            prefix_bundle=prefix,
            profile=profile,
        )
        expected_active = [
            str(row.get("block_id"))
            for row in chapter.get("blocks") or []
            if str(row.get("block_type") or "").casefold()
            not in {"heading", "chapter_heading"}
        ]
        covered = [
            block_id
            for window, _request in windows
            for block_id in window["active_block_ids"]
        ]
        if covered != expected_active or len(covered) != len(set(covered)):
            raise B2ContextError("final B2 V3 windows do not exact-cover active blocks")

        frame_path = f"requests/{_path_component(chapter_id)}/frame_v2_request.json"
        request_rows.append({"relative_path": frame_path, "request": frame_request})
        interaction_index: list[dict[str, Any]] = []
        for ordinal, (window, request) in enumerate(windows, 1):
            relative_path = (
                f"requests/{_path_component(chapter_id)}/"
                f"interaction_v3_{ordinal:02d}_{_path_component(window['window_id'])}.json"
            )
            request_rows.append({"relative_path": relative_path, "request": request})
            interaction_index.append(
                {
                    "window_id": window["window_id"],
                    "window_hash": window["window_hash"],
                    "active_block_ids": list(window["active_block_ids"]),
                    "preceding_tail_block_ids": list(
                        window["preceding_tail_block_ids"]
                    ),
                    "estimated_active_source_tokens": window[
                        "estimated_active_source_tokens"
                    ],
                    "request_path": relative_path,
                    "request_fingerprint": request["request_fingerprint"],
                    "dependency_status": request["dependency_status"],
                    "api_eligible": request["api_eligible"],
                    "token_reserve": deepcopy(request["token_reserve"]),
                }
            )
            prompt_reserve, output_reserve = _request_reserves(request)
            total_prompt_reserve += prompt_reserve
            total_output_reserve += output_reserve
        frame_prompt, frame_output = _request_reserves(frame_request)
        total_prompt_reserve += frame_prompt
        total_output_reserve += frame_output
        chapter_call_count = 1 + len(interaction_index)
        total_calls += chapter_call_count
        chapter_plans.append(
            {
                "chapter_id": chapter_id,
                "chapter_ordinal": chapter_row.get("chapter_ordinal"),
                "source_prefix_bundle_hash": chapter_row.get("prefix_bundle_hash"),
                "frame_request": {
                    "request_path": frame_path,
                    "request_fingerprint": frame_request["request_fingerprint"],
                    "api_eligible": frame_request["api_eligible"],
                    "token_reserve": deepcopy(frame_request["token_reserve"]),
                },
                "interaction_requests": interaction_index,
                "planned_call_count": chapter_call_count,
            }
        )

    plan_body = {
        "schema_version": B2_SLIM_PLAN_SCHEMA_VERSION,
        "phase": "A-D_zero_api",
        "contract_versions": {
            "frame_prompt": "literary_b2_chapter_frame_v5",
            "interaction_prompt": "literary_b2_slim_interaction_window_v11",
        },
        "source_input_hash": real_input.get("input_hash"),
        "source_run_root": real_input.get("source_run_root"),
        "source_plan_hash": real_input.get("source_plan_hash"),
        "source_summary_hash": real_input.get("source_summary_hash"),
        "source_document_sha256": real_input.get("source_document_sha256"),
        "source_run_git_head": real_input.get("source_run_git_head"),
        "current_git_head": real_input.get("current_git_head"),
        "certification_eligible": real_input.get("certification_eligible"),
        "certification_blockers": list(real_input.get("certification_blockers") or []),
        "profile_id": profile.profile_id,
        "profile_hash": profile.profile_hash,
        "profile_path": str(profile.source_path),
        "model_role_defaults": dict(profile.model_role_defaults),
        "ordered_chapter_ids": list(real_input.get("ordered_chapter_ids") or []),
        "chapters": chapter_plans,
        "totals": {
            "planned_requests": total_calls,
            "conservative_prompt_token_reserve": total_prompt_reserve,
            "output_token_reserve": total_output_reserve,
            "conservative_total_token_reserve": total_prompt_reserve
            + total_output_reserve,
            "api_calls_performed": 0,
        },
        "safety": dict(profile.safety),
        "historical_artifact_mutated": False,
        "production_publish_performed": False,
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    return {"plan": plan, "requests": request_rows}


def dry_render_real_b1_run_v3(
    *,
    source_run_root: Path,
    output_root: Path,
    profile: B2PhaseAProfile,
    current_git_head: str,
) -> dict[str, Any]:
    source_root = Path(source_run_root).resolve()
    output = Path(output_root).resolve()
    if output == source_root or _is_within(output, source_root):
        raise B2ContextError("B2 V3 dry-render output may not live inside source run")
    if output.exists():
        raise B2ContextError("B2 V3 dry-render output root must not exist")
    source_tree_before = _tree_hash(source_root)
    real_input = load_b2_source_input_v1(
        source_root, current_git_head=current_git_head
    )
    bundle = build_b2_slim_phase_a_bundle_v1(
        real_input=real_input, profile=profile
    )
    source_tree_after_render = _tree_hash(source_root)
    if source_tree_after_render != source_tree_before:
        raise B2ContextError("source B1 artifact changed during B2 V3 rendering")

    plan = deepcopy(bundle["plan"])
    plan["source_artifact_tree_sha256"] = source_tree_before
    plan_body = dict(plan)
    plan_body.pop("plan_hash", None)
    plan["plan_hash"] = canonical_hash(plan_body)
    report_body = {
        "schema_version": B2_SLIM_PHASE_A_REPORT_SCHEMA_VERSION,
        "status": "complete_zero_api",
        "plan_hash": plan["plan_hash"],
        "source_input_hash": real_input["input_hash"],
        "source_artifact_tree_sha256_before": source_tree_before,
        "source_artifact_tree_sha256_after": source_tree_after_render,
        "source_artifact_mutated": False,
        "certification_eligible": plan["certification_eligible"],
        "certification_blockers": list(plan["certification_blockers"]),
        "chapter_count": len(plan["chapters"]),
        "request_count": plan["totals"]["planned_requests"],
        "token_reserve": deepcopy(plan["totals"]),
        "api_calls_performed": 0,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}

    output.mkdir(parents=True, exist_ok=False)
    _write_new_json(output / "phase_a_plan.json", plan)
    for row in bundle["requests"]:
        relative = Path(_required_string(row.get("relative_path"), "request path"))
        target = (output / relative).resolve()
        if not _is_within(target, output):
            raise B2ContextError("B2 V3 request path escapes output root")
        _write_new_json(target, _mapping(row.get("request"), "request"))
    source_tree_after_write = _tree_hash(source_root)
    if source_tree_after_write != source_tree_before:
        raise B2ContextError("source B1 artifact changed while writing B2 V3 dry render")
    _write_new_json(output / "phase_a_report.json", report)
    return report


def _bounded_interaction_windows_v3(
    *,
    chapter: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    profile: B2PhaseAProfile,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    queue = build_b2_windows_v1(chapter, profile=profile)
    accepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    while queue:
        window = queue.pop(0)
        try:
            request = render_b2_interaction_request_v3(
                window=window,
                prefix_bundle=prefix_bundle,
                profile=profile,
                frame_context=None,
            )
        except B2ContextBudgetError:
            if len(window.get("active_block_ids") or []) <= 1:
                raise
            children = split_b2_window_v1(
                window=window,
                chapter=chapter,
                profile=profile,
            )
            queue = [*children, *queue]
            if len(accepted) + len(queue) > profile.interaction_calls_per_chapter:
                raise B2ContextBudgetError(
                    "adaptive B2 V3 splitting exceeds per-chapter interaction call cap"
                )
            continue
        accepted.append((window, request))
        if len(accepted) + len(queue) > profile.interaction_calls_per_chapter:
            raise B2ContextBudgetError(
                "B2 V3 interaction request count exceeds per-chapter call cap"
            )
    ids = [window["window_id"] for window, _request in accepted]
    if len(ids) != len(set(ids)):
        raise B2ContextError("adaptive B2 V3 splitting produced duplicate window ids")
    return accepted


__all__ = [
    "B2_SLIM_PHASE_A_REPORT_SCHEMA_VERSION",
    "B2_SLIM_PLAN_SCHEMA_VERSION",
    "build_b2_slim_phase_a_bundle_v1",
    "dry_render_real_b1_run_v3",
]
