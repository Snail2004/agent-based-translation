"""Offline: fold validated Auditor decisions into the ledger and project forward.

Zero provider calls.  Input decisions must already have passed the bridge
validators; this script records them against their exact component, queue, and
registry lineage, then writes the reconciled view the next chapter reads.

Outputs, all immutable:

  decision_ledger.json       append-only record of every verdict
  reconciled_projection.json effective entities, settled cases, pending cases
  prior_cards.json           what the next chapter's B1-Scan should consume
  apply_report.json          counts and hashes for the handback
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    B1DecisionLedgerError,
    append_cross_chapter_decisions_v1,
    build_projected_prior_cards_v1,
    empty_decision_ledger_v1,
    project_reconciled_b1_registry_v1,
    verify_decision_ledger_v1,
)


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write(path: Path, payload: object) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-id", required=True)
    parser.add_argument(
        "--registry",
        action="append",
        required=True,
        type=Path,
        help="sealed chapter_registry.json; repeat in author order",
    )
    parser.add_argument(
        "--queue",
        type=Path,
        help="cross_chapter_hearing_queue.json the decisions answer",
    )
    parser.add_argument(
        "--decisions",
        type=Path,
        help="JSON list of validated hearing responses; omit to project only",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        help="existing decision_ledger.json to extend; omit to start a new one",
    )
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    if args.out_dir.exists():
        raise SystemExit(f"out dir already exists (immutable artifacts): {args.out_dir}")
    if bool(args.decisions) != bool(args.queue):
        raise SystemExit("--decisions and --queue must be supplied together")

    registries = []
    for path in args.registry:
        registry = _load(path)
        verify_b1_chapter_registry_v1(registry)
        registries.append(registry)

    ledger = (
        verify_decision_ledger_v1(_load(args.ledger))
        if args.ledger
        else empty_decision_ledger_v1(book_id=args.book_id)
    )
    if ledger["book_id"] != args.book_id:
        raise SystemExit("existing ledger belongs to another book")

    appended = 0
    if args.decisions:
        decisions = _load(args.decisions)
        if not isinstance(decisions, list):
            raise SystemExit("decisions file must hold a JSON list")
        queue = _load(args.queue)
        before = len(ledger["entries"])
        ledger = append_cross_chapter_decisions_v1(
            ledger=ledger, decisions=decisions, queue=queue, registry=registries[-1]
        )
        appended = len(ledger["entries"]) - before

    projection = project_reconciled_b1_registry_v1(registries=registries, ledger=ledger)
    prior_cards = build_projected_prior_cards_v1(
        registries=registries, projection=projection
    )

    args.out_dir.mkdir(parents=True, exist_ok=False)
    # The sealed registries travel with the projection: the next chapter must be
    # able to rebuild the same prior cards and prove it used the same inputs.
    for index, registry in enumerate(registries):
        _write(args.out_dir / f"source_registry_{index:02d}.json", registry)
    _write(args.out_dir / "decision_ledger.json", ledger)
    _write(args.out_dir / "reconciled_projection.json", projection)
    _write(args.out_dir / "prior_cards.json", prior_cards)
    report = {
        "schema_version": "literary_b1_apply_decisions_report_v1",
        "book_id": args.book_id,
        "decisions_appended": appended,
        "ledger_entry_count": len(ledger["entries"]),
        "ledger_hash": ledger["ledger_hash"],
        "projection_hash": projection["projection_hash"],
        "prior_card_count": len(prior_cards),
        "source_registry_hashes": projection["source_registry_hashes"],
        "metrics": projection["metrics"],
        "provider_calls": 0,
        "identity_authority_granted": False,
    }
    _write(args.out_dir / "apply_report.json", report)

    metrics = projection["metrics"]
    print(
        "cross-chapter decisions applied: "
        f"appended={appended} entries={len(ledger['entries'])} "
        f"cards={metrics['source_card_count']} "
        f"effective={metrics['effective_entity_count']} "
        f"merged={metrics['merged_group_count']} "
        f"settled_distinct={metrics['resolved_distinct_count']} "
        f"pending={metrics['pending_case_count']} "
        f"provider_calls=0"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except B1DecisionLedgerError as exc:
        raise SystemExit(f"decision ledger refused the input: {exc}") from exc
