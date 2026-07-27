"""Immutable zero-API writer for Literary B3 temporal Phase A."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from pipeline.literary.b3_temporal_context_v1 import (
    B3TemporalContextError,
    B3TemporalProfileV1,
    build_b3_temporal_phase_a_bundle_v1,
    load_b2_temporal_input_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256


B3_PHASE_A_MULTI_PLAN_SCHEMA_VERSION_V1 = "literary_b3_temporal_multi_plan_v1"
B3_PHASE_A_REPORT_SCHEMA_VERSION_V1 = "literary_b3_temporal_phase_a_report_v1"


def dry_render_b3_temporal_phase_a_v1(
    *,
    b2_run_roots: Sequence[Path],
    speaker_recovery_roots: Sequence[Path | None] = (),
    output_root: Path,
    profile: B3TemporalProfileV1,
) -> dict[str, Any]:
    if not b2_run_roots:
        raise B3TemporalContextError("B3 Phase A requires at least one B2 root")
    output = Path(output_root).resolve()
    if output.exists():
        raise B3TemporalContextError("B3 Phase A output root must not exist")
    roots = [Path(value).resolve() for value in b2_run_roots]
    if len(roots) != len(set(roots)):
        raise B3TemporalContextError("B3 Phase A repeats a B2 root")
    for root in roots:
        if output == root or _is_within(output, root):
            raise B3TemporalContextError("B3 output may not live inside a B2 source root")
    if speaker_recovery_roots and len(speaker_recovery_roots) != len(roots):
        raise B3TemporalContextError(
            "B3 speaker recovery roots must align positionally with B2 roots"
        )
    recovery_roots = (
        [
            Path(value).resolve() if value is not None else None
            for value in speaker_recovery_roots
        ]
        if speaker_recovery_roots
        else [None] * len(roots)
    )
    observed_recovery_roots = [root for root in recovery_roots if root is not None]
    if len(observed_recovery_roots) != len(set(observed_recovery_roots)):
        raise B3TemporalContextError("B3 Phase A repeats a speaker recovery root")
    for root in observed_recovery_roots:
        if output == root or _is_within(output, root):
            raise B3TemporalContextError(
                "B3 output may not live inside a speaker recovery source root"
            )

    all_source_roots = [*roots, *observed_recovery_roots]
    before = {str(root): _tree_hash(root) for root in all_source_roots}
    chapter_rows: list[dict[str, Any]] = []
    bundles: list[dict[str, Any]] = []
    seen_chapters: set[str] = set()
    for root, recovery_root in zip(roots, recovery_roots):
        temporal_input = load_b2_temporal_input_v1(
            root,
            speaker_recovery_root=recovery_root,
        )
        chapter_id = str(temporal_input["chapter_id"])
        if chapter_id in seen_chapters:
            raise B3TemporalContextError("B3 Phase A repeats a chapter")
        seen_chapters.add(chapter_id)
        bundle = build_b3_temporal_phase_a_bundle_v1(
            temporal_input=temporal_input,
            profile=profile,
        )
        bundles.append(
            {"root": root, "temporal_input": temporal_input, "bundle": bundle}
        )
        chapter_rows.append(
            {
                "chapter_id": chapter_id,
                "source_b2_run_root": str(root),
                "source_b2_tree_hash": before[str(root)],
                "source_input_hash": temporal_input["input_hash"],
                "source_b2_artifact_hash": temporal_input["source_b2_artifact_hash"],
                "source_prefix_bundle_hash": temporal_input["source_prefix_bundle_hash"],
                "source_speaker_recovery_root": (
                    str(recovery_root) if recovery_root is not None else None
                ),
                "source_speaker_recovery_tree_hash": (
                    before[str(recovery_root)] if recovery_root is not None else None
                ),
                "source_speaker_recovery_artifact_hash": (
                    temporal_input["speaker_recovery_binding"][
                        "speaker_recovery_artifact_hash"
                    ]
                    if temporal_input["speaker_recovery_binding"] is not None
                    else None
                ),
                "component_count": bundle["plan"]["component_count"],
                "request_count": bundle["plan"]["request_count"],
                "token_reserve": deepcopy(bundle["plan"]["token_reserve"]),
                "chapter_plan_hash": bundle["plan"]["plan_hash"],
            }
        )

    after_render = {str(root): _tree_hash(root) for root in all_source_roots}
    if after_render != before:
        raise B3TemporalContextError("B2 source artifact changed during B3 render")
    multi_body = {
        "schema_version": B3_PHASE_A_MULTI_PLAN_SCHEMA_VERSION_V1,
        "phase": "phase_a_zero_api",
        "profile_id": profile.profile_id,
        "profile_hash": profile.profile_hash,
        "profile_sha256": profile.profile_sha256,
        "ordered_chapter_ids": [row["chapter_id"] for row in chapter_rows],
        "chapters": chapter_rows,
        "totals": {
            "chapters": len(chapter_rows),
            "components": sum(row["component_count"] for row in chapter_rows),
            "planned_requests": sum(row["request_count"] for row in chapter_rows),
            "prompt_token_reserve": sum(
                row["token_reserve"]["prompt_token_reserve"] for row in chapter_rows
            ),
            "output_token_reserve": sum(
                row["token_reserve"]["output_token_reserve"] for row in chapter_rows
            ),
            "api_calls_performed": 0,
        },
        "gold_or_oracle_loaded": False,
        "historical_artifact_mutated": False,
        "production_publish_performed": False,
    }
    multi_plan = {**multi_body, "plan_hash": canonical_hash(multi_body)}

    output.mkdir(parents=True, exist_ok=False)
    _write_new_json(output / "phase_a_plan.json", multi_plan)
    for row in bundles:
        temporal_input = row["temporal_input"]
        bundle = row["bundle"]
        chapter_dir = output / "chapters" / _safe_id(temporal_input["chapter_id"])
        _write_new_json(chapter_dir / "chapter_plan.json", bundle["plan"])
        _write_new_json(
            chapter_dir / "input_manifest.json",
            {
                "schema_version": "literary_b3_temporal_input_manifest_v1",
                "chapter_id": temporal_input["chapter_id"],
                "input_hash": temporal_input["input_hash"],
                "source_b2_run_root": temporal_input["source_b2_run_root"],
                "source_b2_artifact_hash": temporal_input["source_b2_artifact_hash"],
                "source_prefix_bundle_hash": temporal_input["source_prefix_bundle_hash"],
                "speaker_recovery_binding": deepcopy(
                    temporal_input["speaker_recovery_binding"]
                ),
                "candidate_card_count": len(temporal_input["candidate_cards"]),
                "speaker_turn_count": len(temporal_input["speaker_turns"]),
                "salient_event_count": len(temporal_input["salient_events"]),
                "frame_segment_count": len(temporal_input["frame_segments"]),
                "gold_or_oracle_loaded": False,
                "production_publish_performed": False,
            },
        )
        _write_new_json(
            chapter_dir / "components.json",
            {
                "schema_version": "literary_b3_temporal_component_catalog_v1",
                "chapter_id": temporal_input["chapter_id"],
                "components": bundle["components"],
            },
        )
        for index, request in enumerate(bundle["requests"], 1):
            _write_new_json(
                chapter_dir / "requests" / f"{index:02d}_{request['batch_id']}.json",
                request,
            )

    after_write = {str(root): _tree_hash(root) for root in all_source_roots}
    if after_write != before:
        raise B3TemporalContextError("B2 source artifact changed while writing B3 output")
    output_tree_before_report = _tree_hash(output)
    report_body = {
        "schema_version": B3_PHASE_A_REPORT_SCHEMA_VERSION_V1,
        "status": "complete_zero_api",
        "plan_hash": multi_plan["plan_hash"],
        "output_tree_sha256_before_report": output_tree_before_report,
        "ordered_chapter_ids": multi_plan["ordered_chapter_ids"],
        "totals": deepcopy(multi_plan["totals"]),
        "source_tree_hashes_before": before,
        "source_tree_hashes_after": after_write,
        "source_artifact_mutated": False,
        "api_calls_performed": 0,
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_new_json(output / "phase_a_report.json", report)
    return report


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise B3TemporalContextError(f"B3 output already exists: {path}")
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _tree_hash(root: Path) -> str:
    if not root.is_dir():
        raise B3TemporalContextError(f"cannot hash absent tree: {root}")
    rows = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": file_sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return canonical_hash(rows)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_id(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")
    return text or "chapter"


__all__ = [
    "B3_PHASE_A_MULTI_PLAN_SCHEMA_VERSION_V1",
    "B3_PHASE_A_REPORT_SCHEMA_VERSION_V1",
    "dry_render_b3_temporal_phase_a_v1",
]
