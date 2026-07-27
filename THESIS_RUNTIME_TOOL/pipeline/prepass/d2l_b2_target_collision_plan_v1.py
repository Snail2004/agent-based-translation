from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.prepass.d2l_b2_consolidation_plan_v1 import (
    ConsolidationCaps,
    ConsolidationPlanError,
    _candidate_sort_key,
    _component_sort_key,
    _index_counts,
    _normalize_text,
    _sha256_json,
    _to_component_member,
    _to_nonadmitted_ledger,
    _to_provisional_clean,
    _verify_sealed_mapping,
    packetize_components,
    write_plan_artifacts,
)


INDEX_VERSION = "d2l_b2_post_morphology_entry_index_v1"
PLAN_VERSION = "d2l_b2_target_collision_plan_v1"
SELECTION_RULE = (
    "group only post-morphology single-target entries that share an exact "
    "normalized target; shared target opens review and never authorizes merge"
)


def build_post_morphology_index(
    *,
    source_index: Mapping[str, Any],
    stage1_plan: Mapping[str, Any],
    stage1_draft: Mapping[str, Any],
) -> dict[str, Any]:
    _verify_sealed_mapping(source_index, "index_sha256", "source index")
    _verify_sealed_mapping(stage1_plan, "plan_sha256", "stage-1 plan")
    _verify_sealed_mapping(stage1_draft, "draft_sha256", "stage-1 draft")
    if stage1_plan.get("source_index_sha256") != source_index.get(
        "index_sha256"
    ):
        raise ConsolidationPlanError("Stage-1 plan index lineage mismatch")
    if stage1_draft.get("source_index_sha256") != source_index.get(
        "index_sha256"
    ):
        raise ConsolidationPlanError("Stage-1 draft index lineage mismatch")
    if stage1_draft.get("source_plan_sha256") != stage1_plan.get(
        "plan_sha256"
    ):
        raise ConsolidationPlanError("Stage-1 draft plan lineage mismatch")
    if stage1_draft.get("production_published") is not False:
        raise ConsolidationPlanError("Stage-1 draft crossed production boundary")
    if stage1_draft.get("pending_components"):
        raise ConsolidationPlanError("Stage-1 morphology is still pending")
    # B2's sealed index can legitimately carry review/reject ledgers.  The
    # morphology draft must carry those rows forward unchanged; only a drift
    # from the stage-1 plan is invalid.  Rejecting every non-empty ledger here
    # made the all-components-complete path fail after all provider work had
    # already been accepted.
    if (stage1_draft.get("pending_admission") or []) != (
        stage1_plan.get("pending_admission") or []
    ) or (stage1_draft.get("rejected_ledger") or []) != (
        stage1_plan.get("rejected_ledger") or []
    ):
        raise ConsolidationPlanError("Stage-1 draft nonadmitted ledger drift")

    source_rows = {
        str(row["candidate_id"]): row
        for row in source_index.get("decisions") or []
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
        raise ConsolidationPlanError("Source index has no admitted candidates")

    resolved_entries: list[tuple[dict[str, Any], str, str]] = []
    resolved_member_ids: set[str] = set()
    for row in stage1_plan.get("resolved_reuse") or []:
        entry = deepcopy(row.get("reused_entry") or {})
        declared = {str(value) for value in row.get("member_candidate_ids") or []}
        actual = {
            str(value) for value in entry.get("member_candidate_ids") or []
        }
        if not declared or declared != actual:
            raise ConsolidationPlanError("Stage-1 reuse membership drifted")
        if resolved_member_ids.intersection(actual):
            raise ConsolidationPlanError("Stage-1 reuse overlaps another entry")
        _validate_audited_entry(entry=entry, members=admitted)
        resolved_member_ids.update(actual)
        resolved_entries.append(
            (
                entry,
                "reused_prior_audit",
                str(row.get("source_draft_sha256") or ""),
            )
        )

    queued_components = {
        str(row["component_id"]): row
        for row in stage1_plan.get("components") or []
    }
    queued_member_ids = {
        str(member["candidate_id"])
        for component in queued_components.values()
        for member in component.get("members") or []
    }
    live_member_ids: set[str] = set()
    for entry_value in stage1_draft.get("audited_entries") or []:
        entry = deepcopy(entry_value)
        component_id = str(entry.get("component_id") or "")
        component = queued_components.get(component_id)
        if component is None:
            raise ConsolidationPlanError(
                "Stage-1 draft references an unknown queued component"
            )
        expected = {
            str(member["candidate_id"])
            for member in component.get("members") or []
        }
        actual = {
            str(value) for value in entry.get("member_candidate_ids") or []
        }
        if not actual or not actual.issubset(expected):
            raise ConsolidationPlanError("Stage-1 draft membership drifted")
        if live_member_ids.intersection(actual):
            raise ConsolidationPlanError("Stage-1 draft member appears twice")
        _validate_audited_entry(entry=entry, members=admitted)
        live_member_ids.update(actual)
        resolved_entries.append(
            (
                entry,
                "stage1_live_audit",
                str(stage1_draft["draft_sha256"]),
            )
        )
    if live_member_ids != queued_member_ids:
        raise ConsolidationPlanError(
            "Stage-1 draft does not exact-cover queued morphology members"
        )

    deferred_ids = {
        str(value) for value in stage1_plan.get("deferred_candidate_ids") or []
    }
    stage1_parts = [resolved_member_ids, queued_member_ids, deferred_ids]
    for left_index, left in enumerate(stage1_parts):
        for right in stage1_parts[left_index + 1 :]:
            if left.intersection(right):
                raise ConsolidationPlanError("Stage-1 assignment overlaps")
    if set().union(*stage1_parts) != set(admitted):
        raise ConsolidationPlanError(
            "Stage-1 assignment does not exact-cover admitted candidates"
        )

    current_rows: list[dict[str, Any]] = []
    current_member_ids: set[str] = set()
    for entry, authority_kind, authority_hash in resolved_entries:
        member_ids = sorted(
            str(value) for value in entry["member_candidate_ids"]
        )
        if current_member_ids.intersection(member_ids):
            raise ConsolidationPlanError("Current entry membership overlaps")
        current_member_ids.update(member_ids)
        current_rows.append(
            _current_entry_from_audit(
                entry=entry,
                members=admitted,
                source_index_sha256=str(source_index["index_sha256"]),
                authority_kind=authority_kind,
                authority_hash=authority_hash,
            )
        )
    for candidate_id in sorted(deferred_ids):
        current_member_ids.add(candidate_id)
        current_rows.append(
            _current_entry_from_singleton(
                row=admitted[candidate_id],
                source_index_sha256=str(source_index["index_sha256"]),
            )
        )
    if current_member_ids != set(admitted):
        raise ConsolidationPlanError(
            "Post-morphology entries do not exact-cover admitted candidates"
        )

    decisions = sorted(current_rows + nonadmitted, key=_candidate_sort_key)
    payload = {
        "index_version": INDEX_VERSION,
        "chapter_ids": list(source_index.get("chapter_ids") or []),
        "source_index_sha256": source_index["index_sha256"],
        "source_stage1_plan_sha256": stage1_plan["plan_sha256"],
        "source_stage1_draft_sha256": stage1_draft["draft_sha256"],
        "source_full_plan_sha256": stage1_plan.get("source_full_plan_sha256"),
        "source_lineage": deepcopy(source_index.get("source_lineage") or []),
        "decisions": decisions,
        "source_blocks": deepcopy(source_index.get("source_blocks") or []),
        "production_publish_allowed": False,
    }
    payload["counts"] = {
        **_index_counts(decisions),
        "source_admitted_candidates": len(admitted),
        "morphology_entries": len(resolved_entries),
        "morphology_member_candidates": len(
            resolved_member_ids | queued_member_ids
        ),
        "singleton_entries": len(deferred_ids),
        "current_admitted_entries": len(current_rows),
        "source_candidate_exact_cover": len(current_member_ids),
    }
    payload["index_sha256"] = _sha256_json(payload)
    return payload


def build_target_collision_plan(
    current_index: Mapping[str, Any],
) -> dict[str, Any]:
    _verify_sealed_mapping(
        current_index, "index_sha256", "post-morphology entry index"
    )
    admitted = [
        row
        for row in current_index.get("decisions") or []
        if row.get("decision") == "admit"
    ]
    review = [
        row
        for row in current_index.get("decisions") or []
        if row.get("decision") == "review"
    ]
    rejected = [
        row
        for row in current_index.get("decisions") or []
        if row.get("decision") == "reject"
    ]
    by_target: dict[str, list[dict[str, Any]]] = {}
    multi_target: list[dict[str, Any]] = []
    for row in admitted:
        target_keys = {
            _normalize_text(str(target["target_vi"]))
            for target in row.get("target_proposals") or []
        }
        if not target_keys:
            raise ConsolidationPlanError("Current entry has no target proposal")
        if len(target_keys) > 1:
            multi_target.append(deepcopy(row))
            continue
        by_target.setdefault(next(iter(target_keys)), []).append(row)

    components: list[dict[str, Any]] = []
    assigned: set[str] = set()
    clean: list[dict[str, Any]] = []
    for target_key, rows in sorted(by_target.items()):
        ordered = sorted(rows, key=_candidate_sort_key)
        if len(ordered) == 1:
            clean.append(_to_provisional_clean(ordered[0]))
            continue
        member_ids = [str(row["candidate_id"]) for row in ordered]
        edges = [
            {
                "left_candidate_id": member_ids[left_index],
                "right_candidate_id": member_ids[right_index],
                "signals": ["shared_target"],
            }
            for left_index in range(len(member_ids))
            for right_index in range(left_index + 1, len(member_ids))
        ]
        chapter_ids = {str(row["chapter_id"]) for row in ordered}
        if len(chapter_ids) != 1:
            raise ConsolidationPlanError(
                "A target-collision component crosses chapters"
            )
        identity = {
            "stage": "target_collision",
            "source_index_sha256": current_index["index_sha256"],
            "normalized_target": target_key,
            "member_candidate_ids": member_ids,
        }
        components.append(
            {
                "component_id": "targetcmp_"
                + _sha256_json(identity)[:24].lower(),
                "chapter_id": next(iter(chapter_ids)),
                "reason_codes": ["shared_target"],
                "members": [_to_component_member(row) for row in ordered],
                "edges": edges,
                "source_block_ids": sorted(
                    {
                        str(block_id)
                        for row in ordered
                        for block_id in row["evidence_block_ids"]
                    }
                ),
            }
        )
        assigned.update(member_ids)

    clean_ids = {str(row["candidate_id"]) for row in clean}
    multi_ids = {str(row["candidate_id"]) for row in multi_target}
    admitted_ids = {str(row["candidate_id"]) for row in admitted}
    if assigned & clean_ids or assigned & multi_ids or clean_ids & multi_ids:
        raise ConsolidationPlanError("Stage-2 entry assignment overlaps")
    if assigned | clean_ids | multi_ids != admitted_ids:
        raise ConsolidationPlanError(
            "Stage-2 entry assignment does not exact-cover current entries"
        )

    no_review = not components
    plan = {
        "plan_version": PLAN_VERSION,
        "selection_scope": "stage2_exact_target_collision_only",
        "selection_rule": SELECTION_RULE,
        "stage_status": (
            "complete_no_review_required" if no_review else "review_required"
        ),
        "auditor_required": not no_review,
        "production_publish_allowed": False,
        "source_index_sha256": current_index["index_sha256"],
        "source_stage1_plan_sha256": current_index[
            "source_stage1_plan_sha256"
        ],
        "source_stage1_draft_sha256": current_index[
            "source_stage1_draft_sha256"
        ],
        "components": sorted(components, key=_component_sort_key),
        "provisional_clean": sorted(clean, key=_candidate_sort_key),
        "multi_target_deferred": sorted(
            [
                {
                    **_to_provisional_clean(row),
                    "status": "deferred_multi_target",
                }
                for row in multi_target
            ],
            key=_candidate_sort_key,
        ),
        "pending_admission": sorted(
            [_to_nonadmitted_ledger(row) for row in review],
            key=_candidate_sort_key,
        ),
        "rejected_ledger": sorted(
            [_to_nonadmitted_ledger(row) for row in rejected],
            key=_candidate_sort_key,
        ),
        "later_stage_frontier": {
            "multi_target": {
                "status": "ready" if no_review else "blocked",
                "requires": (
                    "sealed_stage2_zero_component_plan"
                    if no_review
                    else "sealed_stage2_target_collision_result"
                ),
                "rule": (
                    "review only entries carrying more than one distinct "
                    "normalized target proposal"
                ),
            }
        },
    }
    plan["counts"] = {
        "current_admitted_entries": len(admitted_ids),
        "target_collision_components": len(components),
        "target_collision_entries": len(assigned),
        "single_target_clean_entries": len(clean_ids),
        "multi_target_deferred_entries": len(multi_ids),
        "current_admitted_exact_cover": len(assigned | clean_ids | multi_ids),
        "pending_admission": len(review),
        "rejected": len(rejected),
    }
    plan["plan_sha256"] = _sha256_json(plan)
    return plan


def prepare_target_collision_artifacts(
    *,
    source_index_path: Path,
    stage1_plan_path: Path,
    stage1_draft_path: Path,
    out_dir: Path,
    caps: ConsolidationCaps,
) -> dict[str, Any]:
    source_index = _read_json(source_index_path)
    stage1_plan = _read_json(stage1_plan_path)
    stage1_draft = _read_json(stage1_draft_path)
    current_index = build_post_morphology_index(
        source_index=source_index,
        stage1_plan=stage1_plan,
        stage1_draft=stage1_draft,
    )
    plan = build_target_collision_plan(current_index)
    packets, dry = packetize_components(
        plan=plan, index=current_index, caps=caps
    )
    write_plan_artifacts(
        out_dir=out_dir,
        index=current_index,
        plan=plan,
        packets=packets,
        dry_render=dry,
    )
    return {
        "status": plan["stage_status"],
        "no_api_called": True,
        "auditor_required": plan["auditor_required"],
        "index_sha256": current_index["index_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "dry_render_sha256": dry["dry_render_sha256"],
        "counts": plan["counts"],
        "dry_totals": dry["totals"],
    }


def _validate_audited_entry(
    *, entry: Mapping[str, Any], members: Mapping[str, Mapping[str, Any]]
) -> None:
    member_ids = [str(value) for value in entry.get("member_candidate_ids") or []]
    if not member_ids or len(member_ids) != len(set(member_ids)):
        raise ConsolidationPlanError("Audited entry has invalid membership")
    if not set(member_ids).issubset(members):
        raise ConsolidationPlanError("Audited entry references unknown member")
    source_rows = [members[candidate_id] for candidate_id in member_ids]
    supplied_sources = {
        str(value)
        for row in source_rows
        for value in [row["canonical_source"], *(row.get("surfaces") or [])]
    }
    supplied_targets = {
        str(target["target_vi"])
        for row in source_rows
        for target in row.get("target_proposals") or []
    }
    supplied_directives = {str(row["directive"]) for row in source_rows}
    supplied_evidence = {
        str(block_id)
        for row in source_rows
        for block_id in row.get("evidence_block_ids") or []
    }
    if entry.get("canonical_source") not in supplied_sources:
        raise ConsolidationPlanError("Audited entry invented a source")
    variants = {str(value) for value in entry.get("source_variants") or []}
    if not variants or not variants.issubset(supplied_sources):
        raise ConsolidationPlanError("Audited entry invented a source variant")
    if entry.get("canonical_target_vi") not in supplied_targets:
        raise ConsolidationPlanError("Audited entry invented a target")
    alternatives = {
        str(value["target_vi"])
        for value in entry.get("alternative_targets") or []
    }
    if not alternatives.issubset(supplied_targets):
        raise ConsolidationPlanError("Audited entry invented an alternative")
    if entry.get("directive") not in supplied_directives:
        raise ConsolidationPlanError("Audited entry invented a directive")
    if {
        str(value) for value in entry.get("evidence_block_ids") or []
    } != supplied_evidence:
        raise ConsolidationPlanError("Audited entry evidence union drifted")
    cited = {
        str(value)
        for value in entry.get("auditor_cited_evidence_block_ids") or []
    }
    if not cited or not cited.issubset(supplied_evidence):
        raise ConsolidationPlanError("Audited entry cited invalid evidence")
    if entry.get("status") != "audited_draft":
        raise ConsolidationPlanError("Audited entry has invalid status")


def _current_entry_from_audit(
    *,
    entry: Mapping[str, Any],
    members: Mapping[str, Mapping[str, Any]],
    source_index_sha256: str,
    authority_kind: str,
    authority_hash: str,
) -> dict[str, Any]:
    member_ids = sorted(str(value) for value in entry["member_candidate_ids"])
    source_rows = [members[candidate_id] for candidate_id in member_ids]
    chapter_ids = {str(row["chapter_id"]) for row in source_rows}
    if len(chapter_ids) != 1:
        raise ConsolidationPlanError("A morphology entry crosses chapters")
    target_proposals = [
        {"target_vi": entry["canonical_target_vi"], "applicability": None},
        *[deepcopy(row) for row in entry.get("alternative_targets") or []],
    ]
    identity = {
        "source_index_sha256": source_index_sha256,
        "member_candidate_ids": member_ids,
    }
    return {
        "candidate_id": "d2lce_" + _sha256_json(identity)[:24].lower(),
        "chapter_id": next(iter(chapter_ids)),
        "surfaces": sorted(
            {
                str(value)
                for row in source_rows
                for value in [row["canonical_source"], *(row.get("surfaces") or [])]
            },
            key=lambda value: (_normalize_text(value), str(value)),
        ),
        "decision": "admit",
        "canonical_source": entry["canonical_source"],
        "target_proposals": target_proposals,
        "directive": entry["directive"],
        "evidence_block_ids": list(entry["evidence_block_ids"]),
        "evidence_complete": all(
            row.get("evidence_complete") is True for row in source_rows
        ),
        "decision_rationale": entry["rationale"],
        "source_member_candidate_ids": member_ids,
        "lineage": {
            "authority_kind": authority_kind,
            "authority_hash": authority_hash,
            "source_index_sha256": source_index_sha256,
            "source_member_candidate_ids": member_ids,
        },
    }


def _current_entry_from_singleton(
    *, row: Mapping[str, Any], source_index_sha256: str
) -> dict[str, Any]:
    member_ids = [str(row["candidate_id"])]
    identity = {
        "source_index_sha256": source_index_sha256,
        "member_candidate_ids": member_ids,
    }
    current = deepcopy(row)
    current["candidate_id"] = "d2lce_" + _sha256_json(identity)[:24].lower()
    current["source_member_candidate_ids"] = member_ids
    current["lineage"] = {
        "authority_kind": "source_b2_singleton",
        "authority_hash": str(row.get("lineage", {}).get("validation_sha256") or ""),
        "source_index_sha256": source_index_sha256,
        "source_member_candidate_ids": member_ids,
    }
    return current


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
        description="Build the post-morphology D2L target-collision frontier"
    )
    parser.add_argument("--source-index", required=True)
    parser.add_argument("--stage1-plan", required=True)
    parser.add_argument("--stage1-draft", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-components", type=int, default=6)
    parser.add_argument("--max-members", type=int, default=16)
    parser.add_argument("--max-unique-blocks", type=int, default=24)
    parser.add_argument("--prompt-token-cap", type=int, default=6000)
    args = parser.parse_args(argv)
    result = prepare_target_collision_artifacts(
        source_index_path=Path(args.source_index),
        stage1_plan_path=Path(args.stage1_plan),
        stage1_draft_path=Path(args.stage1_draft),
        out_dir=Path(args.out),
        caps=ConsolidationCaps(
            max_components=args.max_components,
            max_members=args.max_members,
            max_unique_blocks=args.max_unique_blocks,
            prompt_token_cap=args.prompt_token_cap,
        ),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
