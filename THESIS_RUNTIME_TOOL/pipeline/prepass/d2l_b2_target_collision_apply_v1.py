from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.prepass.d2l_b2_consolidation_plan_v1 import (
    ConsolidationPlanError,
    _candidate_sort_key,
    _index_counts,
    _normalize_text,
    _sha256_json,
    _to_provisional_clean,
    _verify_sealed_mapping,
    _write_or_verify_json,
)
from pipeline.prepass.d2l_b2_target_collision_plan_v1 import (
    _current_entry_from_audit,
    _validate_audited_entry,
)


INDEX_VERSION = "d2l_b2_post_target_collision_entry_index_v1"
PLAN_VERSION = "d2l_b2_target_collision_resolved_plan_v1"
MANIFEST_VERSION = "d2l_b2_target_collision_apply_manifest_v1"


def apply_target_collision_audit(
    *,
    current_index: Mapping[str, Any],
    stage2_plan: Mapping[str, Any],
    stage2_draft: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _verify_sealed_mapping(current_index, "index_sha256", "current index")
    _verify_sealed_mapping(stage2_plan, "plan_sha256", "stage-2 plan")
    _verify_sealed_mapping(stage2_draft, "draft_sha256", "stage-2 draft")
    if stage2_plan.get("source_index_sha256") != current_index.get(
        "index_sha256"
    ):
        raise ConsolidationPlanError("Stage-2 plan index lineage mismatch")
    if stage2_draft.get("source_index_sha256") != current_index.get(
        "index_sha256"
    ):
        raise ConsolidationPlanError("Stage-2 draft index lineage mismatch")
    if stage2_draft.get("source_plan_sha256") != stage2_plan.get(
        "plan_sha256"
    ):
        raise ConsolidationPlanError("Stage-2 draft plan lineage mismatch")
    if stage2_plan.get("production_publish_allowed") is not False:
        raise ConsolidationPlanError("Stage-2 plan crossed publication boundary")
    if stage2_draft.get("production_published") is not False:
        raise ConsolidationPlanError("Stage-2 draft crossed publication boundary")
    if stage2_draft.get("pending_components"):
        raise ConsolidationPlanError("Target-collision audit remains pending")
    if stage2_draft.get("provisional_clean") != stage2_plan.get(
        "provisional_clean"
    ):
        raise ConsolidationPlanError("Stage-2 clean ledger drifted")
    if stage2_draft.get("pending_admission") != stage2_plan.get(
        "pending_admission"
    ):
        raise ConsolidationPlanError("Stage-2 pending ledger drifted")
    if stage2_draft.get("rejected_ledger") != stage2_plan.get(
        "rejected_ledger"
    ):
        raise ConsolidationPlanError("Stage-2 rejected ledger drifted")

    source_rows = {
        str(row["candidate_id"]): row
        for row in current_index.get("decisions") or []
    }
    admitted = {
        candidate_id: row
        for candidate_id, row in source_rows.items()
        if row.get("decision") == "admit"
    }
    nonadmitted = [
        deepcopy(row)
        for row in source_rows.values()
        if row.get("decision") != "admit"
    ]
    if not admitted:
        raise ConsolidationPlanError("Current index has no admitted entries")

    components = {
        str(component["component_id"]): component
        for component in stage2_plan.get("components") or []
    }
    queued_ids: set[str] = set()
    for component in components.values():
        member_ids = {
            str(member["candidate_id"])
            for member in component.get("members") or []
        }
        if not member_ids or not member_ids.issubset(admitted):
            raise ConsolidationPlanError("Stage-2 component membership is invalid")
        if queued_ids.intersection(member_ids):
            raise ConsolidationPlanError("Stage-2 components overlap")
        queued_ids.update(member_ids)

    carried_ids = set(admitted) - queued_ids
    declared_carried_ids = {
        str(row["candidate_id"])
        for row in [
            *(stage2_plan.get("provisional_clean") or []),
            *(stage2_plan.get("multi_target_deferred") or []),
        ]
    }
    if declared_carried_ids != carried_ids:
        raise ConsolidationPlanError(
            "Stage-2 clean and deferred rows do not exact-cover carried entries"
        )

    resolved_rows: list[dict[str, Any]] = []
    seen_queued_ids: set[str] = set()
    seen_components: set[str] = set()
    for entry_value in stage2_draft.get("audited_entries") or []:
        entry = deepcopy(entry_value)
        component_id = str(entry.get("component_id") or "")
        component = components.get(component_id)
        if component is None:
            raise ConsolidationPlanError(
                "Stage-2 draft references an unknown component"
            )
        expected_ids = {
            str(member["candidate_id"])
            for member in component.get("members") or []
        }
        actual_ids = {
            str(value) for value in entry.get("member_candidate_ids") or []
        }
        if not actual_ids or not actual_ids.issubset(expected_ids):
            raise ConsolidationPlanError("Stage-2 draft membership drifted")
        if seen_queued_ids.intersection(actual_ids):
            raise ConsolidationPlanError(
                "Stage-2 draft member appears in multiple audited entries"
            )
        _validate_audited_entry(entry=entry, members=admitted)
        seen_queued_ids.update(actual_ids)
        seen_components.add(component_id)
        resolved_rows.append(
            _resolved_current_entry(
                entry=entry,
                members=admitted,
                source_index_sha256=str(current_index["index_sha256"]),
                source_draft_sha256=str(stage2_draft["draft_sha256"]),
            )
        )
    if seen_queued_ids != queued_ids:
        raise ConsolidationPlanError(
            "Stage-2 draft does not exact-cover queued target collisions"
        )
    if seen_components != set(components):
        raise ConsolidationPlanError(
            "Stage-2 draft does not account for every component"
        )

    carried_rows = [deepcopy(admitted[candidate_id]) for candidate_id in carried_ids]
    decisions = sorted(
        [*resolved_rows, *carried_rows, *nonadmitted], key=_candidate_sort_key
    )
    root_member_ids = {
        str(value)
        for row in [*resolved_rows, *carried_rows]
        for value in (
            row.get("source_member_candidate_ids") or [row["candidate_id"]]
        )
    }
    payload = {
        "index_version": INDEX_VERSION,
        "chapter_ids": list(current_index.get("chapter_ids") or []),
        "source_index_sha256": current_index["index_sha256"],
        "source_stage2_plan_sha256": stage2_plan["plan_sha256"],
        "source_stage2_draft_sha256": stage2_draft["draft_sha256"],
        "source_lineage": deepcopy(current_index.get("source_lineage") or []),
        "decisions": decisions,
        "source_blocks": deepcopy(current_index.get("source_blocks") or []),
        "production_publish_allowed": False,
    }
    payload["counts"] = {
        **_index_counts(decisions),
        "source_admitted_entries": len(admitted),
        "target_collision_source_entries": len(queued_ids),
        "target_collision_resolved_entries": len(resolved_rows),
        "carried_admitted_entries": len(carried_rows),
        "current_admitted_entries": len(resolved_rows) + len(carried_rows),
        "root_member_candidate_exact_cover": len(root_member_ids),
    }
    payload["index_sha256"] = _sha256_json(payload)
    resolved_plan = _build_resolved_plan(
        current_index=payload,
        prior_plan=stage2_plan,
        prior_draft=stage2_draft,
    )
    return payload, resolved_plan


def _resolved_current_entry(
    *,
    entry: Mapping[str, Any],
    members: Mapping[str, Mapping[str, Any]],
    source_index_sha256: str,
    source_draft_sha256: str,
) -> dict[str, Any]:
    current = _current_entry_from_audit(
        entry=entry,
        members=members,
        source_index_sha256=source_index_sha256,
        authority_kind="stage2_target_collision_audit",
        authority_hash=source_draft_sha256,
    )
    stage2_member_ids = list(current["source_member_candidate_ids"])
    root_member_ids = sorted(
        {
            str(value)
            for candidate_id in stage2_member_ids
            for value in (
                members[candidate_id].get("source_member_candidate_ids")
                or [candidate_id]
            )
        }
    )
    current["source_member_candidate_ids"] = root_member_ids
    current["lineage"] = {
        "authority_kind": "stage2_target_collision_audit",
        "authority_hash": source_draft_sha256,
        "source_index_sha256": source_index_sha256,
        "stage2_member_entry_ids": stage2_member_ids,
        "source_member_candidate_ids": root_member_ids,
    }
    return current


def _build_resolved_plan(
    *,
    current_index: Mapping[str, Any],
    prior_plan: Mapping[str, Any],
    prior_draft: Mapping[str, Any],
) -> dict[str, Any]:
    admitted = [
        row
        for row in current_index.get("decisions") or []
        if row.get("decision") == "admit"
    ]
    clean: list[dict[str, Any]] = []
    multi: list[dict[str, Any]] = []
    for row in admitted:
        target_keys = {
            _normalize_text(str(target["target_vi"]))
            for target in row.get("target_proposals") or []
        }
        if not target_keys:
            raise ConsolidationPlanError("Resolved entry has no target proposal")
        projected = _to_provisional_clean(row)
        if len(target_keys) == 1:
            clean.append(projected)
        else:
            multi.append({**projected, "status": "deferred_multi_target"})
    clean.sort(key=_candidate_sort_key)
    multi.sort(key=_candidate_sort_key)
    plan = {
        "plan_version": PLAN_VERSION,
        "selection_scope": "stage2_exact_target_collision_resolved",
        "selection_rule": (
            "apply only sealed target-collision audit decisions; no new "
            "identity or translation judgment is performed by code"
        ),
        "stage_status": "complete_no_review_required",
        "auditor_required": False,
        "production_publish_allowed": False,
        "source_index_sha256": current_index["index_sha256"],
        "source_prior_stage2_plan_sha256": prior_plan["plan_sha256"],
        "source_stage2_draft_sha256": prior_draft["draft_sha256"],
        "components": [],
        "provisional_clean": clean,
        "multi_target_deferred": multi,
        "pending_admission": deepcopy(prior_plan.get("pending_admission") or []),
        "rejected_ledger": deepcopy(prior_plan.get("rejected_ledger") or []),
        "later_stage_frontier": {
            "multi_target": {
                "status": "ready",
                "requires": "sealed_stage2_zero_component_plan",
                "rule": (
                    "review only entries carrying more than one distinct "
                    "normalized target proposal"
                ),
            }
        },
        "counts": {
            "current_admitted_entries": len(admitted),
            "target_collision_components": 0,
            "target_collision_entries": 0,
            "single_target_clean_entries": len(clean),
            "multi_target_deferred_entries": len(multi),
            "current_admitted_exact_cover": len(clean) + len(multi),
            "pending_admission": len(prior_plan.get("pending_admission") or []),
            "rejected": len(prior_plan.get("rejected_ledger") or []),
        },
    }
    plan["plan_sha256"] = _sha256_json(plan)
    return plan


def prepare_resolved_artifacts(
    *,
    current_index_path: Path,
    stage2_plan_path: Path,
    stage2_draft_path: Path,
    out_dir: Path,
) -> dict[str, Any]:
    current_index = _read_json(current_index_path)
    stage2_plan = _read_json(stage2_plan_path)
    stage2_draft = _read_json(stage2_draft_path)
    resolved_index, resolved_plan = apply_target_collision_audit(
        current_index=current_index,
        stage2_plan=stage2_plan,
        stage2_draft=stage2_draft,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_or_verify_json(out_dir / "post_target_collision_index.json", resolved_index)
    _write_or_verify_json(out_dir / "resolved_stage2_plan.json", resolved_plan)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "no_api_called": True,
        "production_publish_performed": False,
        "source_index_sha256": current_index["index_sha256"],
        "source_stage2_plan_sha256": stage2_plan["plan_sha256"],
        "source_stage2_draft_sha256": stage2_draft["draft_sha256"],
        "post_target_collision_index_sha256": resolved_index["index_sha256"],
        "resolved_stage2_plan_sha256": resolved_plan["plan_sha256"],
        "counts": deepcopy(resolved_plan["counts"]),
    }
    manifest["manifest_sha256"] = _sha256_json(manifest)
    _write_or_verify_json(out_dir / "apply_manifest.json", manifest)
    return manifest


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsolidationPlanError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConsolidationPlanError(f"Artifact must be an object: {path}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply a sealed D2L target-collision audit without API calls"
    )
    parser.add_argument("--current-index", required=True)
    parser.add_argument("--stage2-plan", required=True)
    parser.add_argument("--stage2-draft", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    manifest = prepare_resolved_artifacts(
        current_index_path=Path(args.current_index),
        stage2_plan_path=Path(args.stage2_plan),
        stage2_draft_path=Path(args.stage2_draft),
        out_dir=Path(args.out),
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
