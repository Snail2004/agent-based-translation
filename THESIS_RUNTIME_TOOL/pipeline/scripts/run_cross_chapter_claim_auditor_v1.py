from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from pipeline.literary.book_entity_claim_auditor_v1 import (  # noqa: E402
    BookEntityClaimContractError,
    build_prior_claim_projection_v1,
    build_prior_claim_revision_ledger_v1,
    build_prior_claim_ticket_index_v1,
    dry_render_prior_claim_requests_v1,
    render_prior_claim_request_v1,
    validate_prior_claim_response_v1,
)
from pipeline.literary.checkpoint import canonical_json, write_checkpoint_atomic  # noqa: E402


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BookEntityClaimContractError(f"cannot read JSON artifact: {path}") from exc


def _prior_cards(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if isinstance(payload, Mapping):
        payload = payload.get("prior_cards")
    if not isinstance(payload, list):
        raise BookEntityClaimContractError("prior-card file must contain a list")
    return payload


def _challenge_artifacts(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        payload = _load_json(path)
        if not isinstance(payload, dict):
            raise BookEntityClaimContractError(
                f"challenge artifact must be an object: {path}"
            )
        rows.append(payload)
    return rows


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_file():
        existing = _load_json(path)
        if canonical_json(existing) != canonical_json(payload):
            raise BookEntityClaimContractError(
                f"resume artifact differs from existing bytes: {path}"
            )
        return
    write_checkpoint_atomic(path, dict(payload))


def _request_artifact(rendered: Any) -> dict[str, Any]:
    return {
        "component_id": rendered.component_id,
        "request_fingerprint": rendered.request_fingerprint,
        "messages": list(rendered.messages),
        "response_schema": rendered.response_schema,
        "semantic_payload": rendered.semantic_payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and validate bounded cross-chapter prior-claim components."
    )
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--prior-cards", type=Path, required=True)
    parser.add_argument(
        "--challenge-artifact", type=Path, action="append", required=True
    )
    parser.add_argument("--registry-generation-hash", required=True)
    parser.add_argument("--chapter-gists", type=Path)
    parser.add_argument(
        "--design-doc",
        type=Path,
        default=RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--responses-dir",
        type=Path,
        help="Optional directory containing <component_id>.json synthetic/model responses.",
    )
    parser.add_argument("--max-tickets-per-component", type=int, default=4)
    parser.add_argument("--max-involved-chapters", type=int, default=3)
    parser.add_argument("--max-source-blocks", type=int, default=24)
    parser.add_argument("--max-bridge-blocks", type=int, default=8)
    parser.add_argument("--neighbor-radius", type=int, default=1)
    args = parser.parse_args()

    document = _load_json(args.document)
    if not isinstance(document, dict):
        raise BookEntityClaimContractError("document artifact must be an object")
    cards = _prior_cards(args.prior_cards)
    artifacts = _challenge_artifacts(args.challenge_artifact)
    chapter_gists = _load_json(args.chapter_gists) if args.chapter_gists else None
    index = build_prior_claim_ticket_index_v1(
        document=document,
        prior_cards=cards,
        challenge_artifacts=artifacts,
        registry_generation_hash=args.registry_generation_hash,
        chapter_gists=chapter_gists,
        max_tickets_per_component=args.max_tickets_per_component,
        max_involved_chapters=args.max_involved_chapters,
        max_source_blocks=args.max_source_blocks,
        max_bridge_blocks=args.max_bridge_blocks,
        neighbor_radius=args.neighbor_radius,
    )
    output_dir = args.output_dir.resolve()
    _write_immutable(output_dir / "ticket_index.json", index)

    dry_report = dry_render_prior_claim_requests_v1(
        index=index,
        document=document,
        design_doc=args.design_doc,
    )
    _write_immutable(output_dir / "dry_render_report.json", dry_report)

    decisions: list[dict[str, Any]] = []
    component_status: list[dict[str, Any]] = []
    for component in index["claim_components"]:
        component_id = component["component_id"]
        if component["overflow"]:
            component_status.append(
                {
                    "component_id": component_id,
                    "status": "pending_overflow",
                    "overflow_reasons": component["overflow_reasons"],
                }
            )
            continue
        rendered = render_prior_claim_request_v1(
            index=index,
            component_id=component_id,
            document=document,
            design_doc=args.design_doc,
        )
        component_dir = output_dir / "components" / component_id
        _write_immutable(component_dir / "request.json", _request_artifact(rendered))
        response_path = (
            args.responses_dir / f"{component_id}.json"
            if args.responses_dir is not None
            else None
        )
        if response_path is None or not response_path.is_file():
            component_status.append(
                {
                    "component_id": component_id,
                    "status": "awaiting_response",
                    "request_fingerprint": rendered.request_fingerprint,
                }
            )
            continue
        response = _load_json(response_path)
        if not isinstance(response, dict):
            raise BookEntityClaimContractError(
                f"component response must be an object: {response_path}"
            )
        decision = validate_prior_claim_response_v1(
            response,
            index=index,
            request_fingerprint=rendered.request_fingerprint,
        )
        _write_immutable(component_dir / "decision.json", decision)
        decisions.append(decision)
        component_status.append(
            {
                "component_id": component_id,
                "status": "validated",
                "request_fingerprint": rendered.request_fingerprint,
                "decision_hash": decision["decision_hash"],
            }
        )

    all_renderable_validated = len(decisions) == sum(
        not component["overflow"] for component in index["claim_components"]
    )
    run_plan = {
        "schema_version": "cross_chapter_claim_audit_run_plan_v1",
        "ticket_index_hash": index["ticket_index_hash"],
        "semantic_halt_required": False,
        "preflight_pending_ticket_ids": index["preflight_pending_ticket_ids"],
        "identity_referrals": index["identity_referrals"],
        "components": sorted(component_status, key=lambda row: row["component_id"]),
        "all_renderable_components_validated": all_renderable_validated,
    }
    _write_immutable(output_dir / "run_plan.json", run_plan)

    if all_renderable_validated:
        ledger = build_prior_claim_revision_ledger_v1(
            index=index,
            decisions=decisions,
        )
        projection = build_prior_claim_projection_v1(
            prior_cards=cards,
            ledger=ledger,
        )
        _write_immutable(output_dir / "claim_revision_ledger.json", ledger)
        _write_immutable(output_dir / "claim_projection.json", projection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
