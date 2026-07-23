from __future__ import annotations

import argparse
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.prepass.d2l_b2_consolidation_plan_v1 import (
    ConsolidationPlanError,
    _candidate_sort_key,
    _normalize_text,
    _sha256_json,
    _text_sort_key,
    _to_provisional_clean,
    _verify_sealed_mapping,
    _write_or_verify_json,
)
from pipeline.prepass.d2l_b2_multi_target_contract_v1 import (
    PROMPT_VERSION,
    RESPONSE_FORMAT,
    prompt_sha256,
    render_messages,
    response_schema_sha256,
    user_payload_sha256,
)


PLAN_VERSION = "d2l_b2_multi_target_plan_v1"
DRY_RENDER_VERSION = "d2l_b2_multi_target_dry_render_v1"
SELECTION_RULE = (
    "review each admitted current entry with more than one distinct normalized "
    "target proposal; packet co-location reuses source context and never joins "
    "entry authority"
)


@dataclass(frozen=True)
class MultiTargetCaps:
    max_items: int = 4
    max_target_proposals: int = 10
    max_unique_blocks: int = 18
    prompt_token_cap: int = 6000

    def validate(self) -> None:
        values = {
            "max_items": self.max_items,
            "max_target_proposals": self.max_target_proposals,
            "max_unique_blocks": self.max_unique_blocks,
            "prompt_token_cap": self.prompt_token_cap,
        }
        for name, value in values.items():
            if not isinstance(value, int) or value <= 0:
                raise ConsolidationPlanError(f"{name} must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        self.validate()
        return {
            "max_items": self.max_items,
            "max_target_proposals": self.max_target_proposals,
            "max_unique_blocks": self.max_unique_blocks,
            "prompt_token_cap": self.prompt_token_cap,
        }


def build_multi_target_plan(
    *, current_index: Mapping[str, Any], stage2_plan: Mapping[str, Any]
) -> dict[str, Any]:
    _verify_sealed_mapping(
        current_index, "index_sha256", "post-morphology entry index"
    )
    _verify_sealed_mapping(stage2_plan, "plan_sha256", "stage-2 plan")
    if stage2_plan.get("source_index_sha256") != current_index.get(
        "index_sha256"
    ):
        raise ConsolidationPlanError("Stage-2 plan index lineage mismatch")
    if stage2_plan.get("stage_status") != "complete_no_review_required":
        raise ConsolidationPlanError("Stage-2 target collision is not complete")
    if stage2_plan.get("auditor_required") is not False or stage2_plan.get(
        "components"
    ):
        raise ConsolidationPlanError(
            "Stage-2 target collision still requires review"
        )
    frontier = (stage2_plan.get("later_stage_frontier") or {}).get(
        "multi_target"
    )
    if not isinstance(frontier, dict) or frontier.get("status") != "ready":
        raise ConsolidationPlanError("Stage-2 multi-target frontier is not ready")
    if frontier.get("requires") != "sealed_stage2_zero_component_plan":
        raise ConsolidationPlanError("Stage-2 frontier authority is invalid")

    admitted = [
        row
        for row in current_index.get("decisions") or []
        if row.get("decision") == "admit"
    ]
    review_rows: list[dict[str, Any]] = []
    clean_rows: list[dict[str, Any]] = []
    for row in admitted:
        target_keys = {
            _normalize_text(str(target["target_vi"]))
            for target in row.get("target_proposals") or []
        }
        if not target_keys:
            raise ConsolidationPlanError("Current entry has no target proposal")
        if len(target_keys) > 1:
            review_rows.append(deepcopy(row))
        else:
            clean_rows.append(deepcopy(row))

    expected_deferred = sorted(
        [
            {
                **_to_provisional_clean(row),
                "status": "deferred_multi_target",
            }
            for row in review_rows
        ],
        key=_candidate_sort_key,
    )
    if stage2_plan.get("multi_target_deferred") != expected_deferred:
        raise ConsolidationPlanError(
            "Stage-2 deferred multi-target rows drifted from current index"
        )
    expected_clean = sorted(
        [_to_provisional_clean(row) for row in clean_rows],
        key=_candidate_sort_key,
    )
    if stage2_plan.get("provisional_clean") != expected_clean:
        raise ConsolidationPlanError(
            "Stage-2 provisional-clean rows drifted from current index"
        )

    review_items = sorted(
        [
            {
                "candidate_id": row["candidate_id"],
                "status": "review_required",
                "chapter_id": row["chapter_id"],
                "canonical_source": row["canonical_source"],
                "surfaces": deepcopy(row["surfaces"]),
                "target_proposals": deepcopy(row["target_proposals"]),
                "directive": row["directive"],
                "evidence_block_ids": deepcopy(row["evidence_block_ids"]),
                "reason_codes": ["multiple_distinct_target_proposals"],
                "lineage": deepcopy(row["lineage"]),
            }
            for row in review_rows
        ],
        key=_candidate_sort_key,
    )
    review_ids = {str(row["candidate_id"]) for row in review_items}
    clean_ids = {str(row["candidate_id"]) for row in clean_rows}
    admitted_ids = {str(row["candidate_id"]) for row in admitted}
    if review_ids & clean_ids or review_ids | clean_ids != admitted_ids:
        raise ConsolidationPlanError(
            "Stage-3 assignment does not exact-cover current admitted entries"
        )

    plan = {
        "plan_version": PLAN_VERSION,
        "selection_scope": "stage3_multi_target_entries_only",
        "selection_rule": SELECTION_RULE,
        "stage_status": (
            "review_required" if review_items else "complete_no_review_required"
        ),
        "auditor_required": bool(review_items),
        "production_publish_allowed": False,
        "source_index_sha256": current_index["index_sha256"],
        "source_stage2_plan_sha256": stage2_plan["plan_sha256"],
        "review_items": review_items,
        "provisional_clean": expected_clean,
        "pending_admission": deepcopy(stage2_plan.get("pending_admission") or []),
        "rejected_ledger": deepcopy(stage2_plan.get("rejected_ledger") or []),
        "next_stage_frontier": {
            "publication": {
                "status": "blocked" if review_items else "ready",
                "requires": (
                    "sealed_stage3_auditor_result"
                    if review_items
                    else "sealed_stage3_zero_item_plan"
                ),
            }
        },
        "counts": {
            "current_admitted_entries": len(admitted_ids),
            "multi_target_review_entries": len(review_ids),
            "single_target_clean_entries": len(clean_ids),
            "current_admitted_exact_cover": len(review_ids | clean_ids),
            "target_proposals_under_review": sum(
                len(row["target_proposals"]) for row in review_items
            ),
            "pending_admission": len(stage2_plan.get("pending_admission") or []),
            "rejected": len(stage2_plan.get("rejected_ledger") or []),
        },
    }
    plan["plan_sha256"] = _sha256_json(plan)
    return plan


def packetize_multi_target_items(
    *, plan: Mapping[str, Any], index: Mapping[str, Any], caps: MultiTargetCaps
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    caps.validate()
    _verify_sealed_mapping(plan, "plan_sha256", "multi-target plan")
    _verify_sealed_mapping(index, "index_sha256", "consolidation index")
    if plan.get("source_index_sha256") != index.get("index_sha256"):
        raise ConsolidationPlanError("Plan and index lineage do not match")

    block_map: dict[str, str] = {}
    for row in index.get("source_blocks") or []:
        block_id = str(row.get("block_id") or "")
        text = row.get("text")
        if not block_id or block_id in block_map or not isinstance(text, str):
            raise ConsolidationPlanError("Source block index is invalid")
        block_map[block_id] = text

    remaining = [deepcopy(row) for row in plan.get("review_items") or []]
    packets: list[dict[str, Any]] = []
    while remaining:
        remaining.sort(key=_candidate_sort_key)
        current = [remaining.pop(0)]
        single = _make_packet(current, block_map, str(plan["plan_sha256"]))
        if not _packet_fits(single, caps):
            raise ConsolidationPlanError(
                f"Review item cannot fit packet caps: {current[0]['candidate_id']}"
            )
        while remaining and len(current) < caps.max_items:
            current_blocks = {
                str(block_id)
                for row in current
                for block_id in row["evidence_block_ids"]
            }
            ranked = sorted(
                enumerate(remaining),
                key=lambda pair: _packing_sort_key(pair[1], current_blocks),
            )
            selected_index: int | None = None
            for candidate_index, candidate in ranked:
                if candidate["chapter_id"] != current[0]["chapter_id"]:
                    continue
                trial = _make_packet(
                    [*current, candidate], block_map, str(plan["plan_sha256"])
                )
                if _packet_fits(trial, caps):
                    selected_index = candidate_index
                    break
            if selected_index is None:
                break
            current.append(remaining.pop(selected_index))
        packets.append(_make_packet(current, block_map, str(plan["plan_sha256"])))

    summaries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for packet in packets:
        messages = render_messages(packet)
        prompt_tokens = estimate_prompt_tokens(messages, RESPONSE_FORMAT)
        if not _packet_fits(packet, caps, prompt_tokens=prompt_tokens):
            raise ConsolidationPlanError(
                f"Final packet violates caps: {packet['packet_id']}"
            )
        candidate_ids = [
            str(row["candidate_id"]) for row in packet["review_items"]
        ]
        if seen_ids.intersection(candidate_ids):
            raise ConsolidationPlanError("Review item appears in multiple packets")
        seen_ids.update(candidate_ids)
        summaries.append(
            {
                "packet_id": packet["packet_id"],
                "chapter_id": packet["chapter_id"],
                "candidate_ids": candidate_ids,
                "review_item_count": len(candidate_ids),
                "target_proposal_count": sum(
                    len(row["target_proposals"])
                    for row in packet["review_items"]
                ),
                "source_block_ids": [
                    row["block_id"] for row in packet["source_blocks"]
                ],
                "source_block_count": len(packet["source_blocks"]),
                "prompt_tokens_est": prompt_tokens,
                "user_payload_sha256": user_payload_sha256(messages),
            }
        )
    expected_ids = {
        str(row["candidate_id"]) for row in plan.get("review_items") or []
    }
    if seen_ids != expected_ids:
        raise ConsolidationPlanError("Packets do not exact-cover review items")

    all_block_ids = {
        str(block_id)
        for row in plan.get("review_items") or []
        for block_id in row["evidence_block_ids"]
    }
    dry = {
        "dry_render_version": DRY_RENDER_VERSION,
        "no_api_called": True,
        "source_index_sha256": index["index_sha256"],
        "source_plan_sha256": plan["plan_sha256"],
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "response_schema_sha256": response_schema_sha256(),
        "caps": caps.to_dict(),
        "packets": summaries,
        "totals": {
            "packet_count": len(packets),
            "review_item_count": len(seen_ids),
            "target_proposal_count": sum(
                row["target_proposal_count"] for row in summaries
            ),
            "distinct_source_blocks": len(all_block_ids),
            "source_block_renders": sum(
                row["source_block_count"] for row in summaries
            ),
            "source_block_reuse_savings": (
                sum(
                    len(row["evidence_block_ids"])
                    for row in plan.get("review_items") or []
                )
                - sum(row["source_block_count"] for row in summaries)
            ),
            "prompt_tokens_est": sum(
                row["prompt_tokens_est"] for row in summaries
            ),
            "max_packet_prompt_tokens_est": max(
                (row["prompt_tokens_est"] for row in summaries), default=0
            ),
        },
    }
    dry["dry_render_sha256"] = _sha256_json(dry)
    return packets, dry


def write_multi_target_artifacts(
    *,
    out_dir: Path,
    index: Mapping[str, Any],
    stage2_plan: Mapping[str, Any],
    plan: Mapping[str, Any],
    packets: Sequence[Mapping[str, Any]],
    dry_render: Mapping[str, Any],
) -> None:
    _write_or_verify_json(out_dir / "source_index.json", index)
    _write_or_verify_json(out_dir / "source_stage2_plan.json", stage2_plan)
    _write_or_verify_json(out_dir / "multi_target_plan.json", plan)
    summaries = {
        str(row["packet_id"]): row for row in dry_render["packets"]
    }
    for packet in packets:
        messages = render_messages(packet)
        request = {
            "messages": messages,
            "packet": packet,
            "response_format": RESPONSE_FORMAT,
            "summary": summaries[str(packet["packet_id"])],
        }
        _write_or_verify_json(
            out_dir / "packets" / str(packet["packet_id"]) / "request.json",
            request,
        )
    _write_or_verify_json(out_dir / "dry_render.json", dry_render)


def prepare_multi_target_artifacts(
    *, index_path: Path, stage2_plan_path: Path, out_dir: Path, caps: MultiTargetCaps
) -> dict[str, Any]:
    index = _read_json(index_path)
    stage2_plan = _read_json(stage2_plan_path)
    plan = build_multi_target_plan(
        current_index=index, stage2_plan=stage2_plan
    )
    packets, dry = packetize_multi_target_items(
        plan=plan, index=index, caps=caps
    )
    write_multi_target_artifacts(
        out_dir=out_dir,
        index=index,
        stage2_plan=stage2_plan,
        plan=plan,
        packets=packets,
        dry_render=dry,
    )
    return {
        "status": plan["stage_status"],
        "no_api_called": True,
        "auditor_required": plan["auditor_required"],
        "plan_sha256": plan["plan_sha256"],
        "dry_render_sha256": dry["dry_render_sha256"],
        "counts": plan["counts"],
        "dry_totals": dry["totals"],
    }


def _make_packet(
    items: Sequence[Mapping[str, Any]],
    block_map: Mapping[str, str],
    plan_sha256: str,
) -> dict[str, Any]:
    if not items:
        raise ConsolidationPlanError("Cannot create an empty packet")
    chapter_ids = {str(row["chapter_id"]) for row in items}
    if len(chapter_ids) != 1:
        raise ConsolidationPlanError("A packet cannot cross chapter boundaries")
    block_ids = sorted(
        {
            str(block_id)
            for row in items
            for block_id in row["evidence_block_ids"]
        },
        key=_text_sort_key,
    )
    missing = [block_id for block_id in block_ids if block_id not in block_map]
    if missing:
        raise ConsolidationPlanError(
            "Review item cites unavailable blocks: " + ", ".join(missing)
        )
    projected_items = [
        {
            key: deepcopy(value)
            for key, value in row.items()
            if key not in {"chapter_id", "status", "lineage"}
        }
        for row in items
    ]
    identity = {
        "plan_version": PLAN_VERSION,
        "plan_sha256": plan_sha256,
        "chapter_id": next(iter(chapter_ids)),
        "candidate_ids": [row["candidate_id"] for row in projected_items],
        "source_block_ids": block_ids,
        "prompt_sha256": prompt_sha256(),
        "response_schema_sha256": response_schema_sha256(),
    }
    return {
        "packet_id": "mtpkt_" + _sha256_json(identity)[:24].lower(),
        "chapter_id": next(iter(chapter_ids)),
        "review_items": projected_items,
        "source_blocks": [
            {"block_id": block_id, "text": block_map[block_id]}
            for block_id in block_ids
        ],
    }


def _packet_fits(
    packet: Mapping[str, Any],
    caps: MultiTargetCaps,
    *,
    prompt_tokens: int | None = None,
) -> bool:
    if prompt_tokens is None:
        prompt_tokens = estimate_prompt_tokens(
            render_messages(packet), RESPONSE_FORMAT
        )
    return (
        len(packet["review_items"]) <= caps.max_items
        and sum(
            len(row["target_proposals"]) for row in packet["review_items"]
        )
        <= caps.max_target_proposals
        and len(packet["source_blocks"]) <= caps.max_unique_blocks
        and prompt_tokens <= caps.prompt_token_cap
    )


def _packing_sort_key(
    row: Mapping[str, Any], current_blocks: set[str]
) -> tuple[Any, ...]:
    blocks = {str(value) for value in row["evidence_block_ids"]}
    shared = len(blocks & current_blocks)
    added = len(blocks - current_blocks)
    return (
        -shared,
        added,
        _normalize_text(str(row["canonical_source"])),
        str(row["candidate_id"]),
    )


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
        description="Build the deterministic 0-API D2L multi-target frontier"
    )
    parser.add_argument("--index", required=True)
    parser.add_argument("--stage2-plan", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-items", type=int, default=4)
    parser.add_argument("--max-target-proposals", type=int, default=10)
    parser.add_argument("--max-unique-blocks", type=int, default=18)
    parser.add_argument("--prompt-token-cap", type=int, default=6000)
    args = parser.parse_args(argv)
    result = prepare_multi_target_artifacts(
        index_path=Path(args.index),
        stage2_plan_path=Path(args.stage2_plan),
        out_dir=Path(args.out),
        caps=MultiTargetCaps(
            max_items=args.max_items,
            max_target_proposals=args.max_target_proposals,
            max_unique_blocks=args.max_unique_blocks,
            prompt_token_cap=args.prompt_token_cap,
        ),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
