from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    bind_b2_review_routing_to_hearing_queue_v1,
)
from pipeline.literary.b1_registry_to_b2_input_v1 import (
    verify_b2_registry_input_package_v1,
)
from pipeline.literary.b2_review_resolution_v1 import (
    build_review_routing_plan_from_artifacts_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected one JSON object: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _review_scope_from_b2_input_v1(
    *,
    package: dict[str, Any],
    chapter_id: str,
    registry_hash: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    chapter_rows = [
        row
        for row in package["chapters"]
        if row.get("chapter_id") == chapter_id
    ]
    if len(chapter_rows) != 1:
        raise ValueError("B2 input package lacks the routed chapter")
    if (
        chapter_rows[0].get("source_registry", {}).get("registry_hash")
        != registry_hash
    ):
        raise ValueError("B2 input package and routed registry differ")
    projection = package.get("reconciled_projection")
    if projection is None:
        ordered_chapter_ids = package.get("ordered_chapter_ids")
        if ordered_chapter_ids != [chapter_id]:
            raise ValueError(
                "multi-chapter B2 input package has no reconciled projection"
            )
        return [], []
    if not isinstance(projection, dict):
        raise ValueError("B2 input package has malformed reconciled projection")
    raw_effective = projection.get("effective_entities")
    if not isinstance(raw_effective, list):
        raise ValueError(
            "B2 reconciled projection has no effective entity list"
        )
    candidate_scope_cards = [
        dict(row) for row in raw_effective if isinstance(row, dict)
    ]
    if len(candidate_scope_cards) != len(raw_effective):
        raise ValueError("B2 effective entity list is malformed")
    known_ids = {
        str(row.get("effective_entity_id") or row.get("entity_id") or "")
        for row in candidate_scope_cards
    }
    prefix_bundle = chapter_rows[0].get("prefix_bundle")
    if not isinstance(prefix_bundle, dict):
        raise ValueError("B2 routed chapter has no prefix bundle")
    prefix_cards = list(prefix_bundle.get("b0_context_cards") or []) + list(
        prefix_bundle.get("candidate_only_context_cards") or []
    )
    seen_prefix_ids: set[str] = set()
    for raw_card in prefix_cards:
        if not isinstance(raw_card, dict):
            raise ValueError("B2 prefix candidate card is malformed")
        card_id = raw_card.get("prior_card_id")
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("B2 prefix candidate card has no prior_card_id")
        if card_id in seen_prefix_ids:
            raise ValueError("B2 prefix candidate card id repeats")
        seen_prefix_ids.add(card_id)
        if card_id in known_ids:
            continue
        provenance = raw_card.get("provenance_refs")
        if (
            not isinstance(provenance, list)
            or not provenance
            or not all(isinstance(row, dict) for row in provenance)
        ):
            raise ValueError(
                f"B2 prefix candidate card has malformed provenance: {card_id}"
            )
        first_supported = raw_card.get("first_supported_block_id")
        first_ref = next(
            (
                row
                for row in provenance
                if first_supported
                and row.get("block_id") == first_supported
            ),
            provenance[0],
        )
        first_chapter_id = first_ref.get("chapter_id")
        if not isinstance(first_chapter_id, str) or not first_chapter_id:
            raise ValueError(
                f"B2 prefix candidate card has no first chapter: {card_id}"
            )
        card = dict(raw_card)
        card["entity_id"] = card_id
        card["first_seen"] = {
            "chapter_id": first_chapter_id,
            "block_id": first_ref.get("block_id"),
        }
        candidate_scope_cards.append(card)
        known_ids.add(card_id)
    raw_superseded = projection.get("superseded_pending_cases") or []
    if not isinstance(raw_superseded, list) or not all(
        isinstance(row, dict) for row in raw_superseded
    ):
        raise ValueError(
            "B2 reconciled projection has malformed superseded pending cases"
        )
    superseded_cross_component_ids = sorted(
        {
            str(row["component_id"])
            for row in raw_superseded
            if isinstance(row.get("component_id"), str)
            and row["component_id"]
        }
    )
    if len(superseded_cross_component_ids) != len(raw_superseded):
        raise ValueError(
            "B2 superseded pending cases repeat or omit component ids"
        )
    return candidate_scope_cards, superseded_cross_component_ids


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Route typed Literary B2 reviews without calling a model."
    )
    parser.add_argument("--b2-root", type=Path, required=True)
    parser.add_argument(
        "--b2-input-root",
        type=Path,
        help=(
            "sealed B1-to-B2 package whose reconciled projection supplies "
            "carried candidate cards"
        ),
    )
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--local-audit-root", type=Path, required=True)
    parser.add_argument("--hearing-queue-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--decided-cross-component-id",
        action="append",
        default=[],
    )
    args = parser.parse_args()

    output = args.output_root.resolve()
    if output.exists():
        raise FileExistsError(f"output root already exists: {output}")
    b2_artifact = _read(
        args.b2_root.resolve() / "chapter_b2_artifact.json"
    )
    chapter_registry = _read(
        args.registry_root.resolve() / "chapter_registry.json"
    )
    local_audit_artifact = _read(
        args.local_audit_root.resolve() / "local_audit_artifact.json"
    )
    hearing_queue = _read(
        args.hearing_queue_root.resolve() / "cross_chapter_hearing_queue.json"
    )
    candidate_scope_cards: list[dict[str, Any]] = []
    superseded_cross_component_ids: list[str] = []
    if args.b2_input_root is not None:
        package = verify_b2_registry_input_package_v1(
            _read(args.b2_input_root.resolve() / "b2_registry_input.json")
        )
        (
            candidate_scope_cards,
            superseded_cross_component_ids,
        ) = _review_scope_from_b2_input_v1(
            package=package,
            chapter_id=str(b2_artifact.get("chapter_id") or ""),
            registry_hash=str(chapter_registry.get("registry_hash") or ""),
        )
    plan = build_review_routing_plan_from_artifacts_v1(
        b2_artifact=b2_artifact,
        chapter_registry=chapter_registry,
        local_audit_artifact=local_audit_artifact,
        hearing_queue=hearing_queue,
        decided_cross_component_ids=args.decided_cross_component_id,
        superseded_cross_component_ids=superseded_cross_component_ids,
        candidate_scope_cards=candidate_scope_cards,
    )
    routed_queue = bind_b2_review_routing_to_hearing_queue_v1(
        hearing_queue=hearing_queue,
        routing_plan=plan,
        chapter_registry=chapter_registry,
        b2_artifact=b2_artifact,
        candidate_scope_cards=candidate_scope_cards,
    )
    _write(output / "review_routing_plan.json", plan)
    _write(output / "cross_chapter_hearing_queue.json", routed_queue)
    report_body = {
        "schema_version": "literary_b2_review_routing_report_v1",
        "chapter_id": plan["chapter_id"],
        "routing_plan_hash": plan["routing_plan_hash"],
        "routed_hearing_queue_hash": routed_queue["queue_hash"],
        "counts": {
            route: len(plan[f"route_{route.casefold()}"])
            for route in ("A", "B", "C", "D")
        },
        "model_calls_performed": 0,
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
    }
    _write(
        output / "routing_report.json",
        {**report_body, "report_hash": canonical_hash(report_body)},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
