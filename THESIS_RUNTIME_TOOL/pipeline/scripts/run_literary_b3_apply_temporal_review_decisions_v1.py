"""Apply verified B3 temporal review overlays offline, with zero provider calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.literary.b3_temporal_decision_ledger_v1 import (
    B3TemporalDecisionLedgerError,
    fold_b3_temporal_review_overlays_v1,
    merge_b3_narrative_frame_catalogs_v1,
)
from pipeline.literary.b3_temporal_prefix_v1 import (
    B3TemporalPrefixError,
    load_b3_temporal_chapter_artifact_v1,
)
from pipeline.literary.checkpoint import (
    CheckpointError,
    resolve_existing_canonical_path,
)


def _read(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise B3TemporalDecisionLedgerError(f"JSON object required: {path}")
    return value


def _write(path: Path, value: object) -> None:
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1, sort_keys=True)


def _resolve_source_root(raw_path: object) -> Path:
    """Resolve paths stored in canonical JSON without losing filesystem spelling."""
    try:
        return resolve_existing_canonical_path(str(raw_path or ""))
    except CheckpointError as exc:
        raise B3TemporalDecisionLedgerError(
            f"B3 prior temporal source root is absent: {raw_path}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b3-root", required=True, type=Path)
    parser.add_argument("--overlay", action="append", default=[], type=Path)
    parser.add_argument("--frame-catalog", action="append", default=[], type=Path)
    parser.add_argument("--component-catalog", action="append", default=[], type=Path)
    parser.add_argument("--reconciled-projection", type=Path)
    parser.add_argument("--consolidate-only", action="store_true")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.out_dir.exists():
        raise SystemExit(f"out dir already exists (immutable artifacts): {args.out_dir}")
    chapter_artifact, _artifact_path = load_b3_temporal_chapter_artifact_v1(
        args.b3_root
    )
    frame_catalog_inputs = []
    identity_component_catalogs = []
    component_catalog_path = args.b3_root / "component_catalog.json"
    if component_catalog_path.exists():
        current_component_catalog = _read(component_catalog_path)
        frame_catalog_inputs.append(current_component_catalog)
        identity_component_catalogs.append(current_component_catalog)
    prefix_path = args.b3_root / "prior_temporal_prefix.json"
    if prefix_path.exists():
        prefix = _read(prefix_path)
        for source in prefix.get("source_chapters") or []:
            if not isinstance(source, dict):
                raise B3TemporalDecisionLedgerError(
                    "B3 prior temporal source row is malformed"
                )
            source_root = _resolve_source_root(source.get("source_root"))
            prior_artifact, _ = load_b3_temporal_chapter_artifact_v1(source_root)
            inherited_catalog = prior_artifact.get("frame_catalog")
            if inherited_catalog is not None:
                frame_catalog_inputs.append(inherited_catalog)
    frame_catalog_inputs.extend(_read(path) for path in args.frame_catalog)
    identity_component_catalogs.extend(
        _read(path) for path in args.component_catalog
    )
    frame_catalog = merge_b3_narrative_frame_catalogs_v1(frame_catalog_inputs)
    overlay_packets = []
    for root in args.overlay:
        if not root.is_dir():
            raise B3TemporalDecisionLedgerError(f"overlay root is absent: {root}")
        overlay_packets.append(
            (
                _read(root / "temporal_review_overlay.json"),
                _read(root / "review_packet.json"),
            )
        )
    identity_projection = (
        _read(args.reconciled_projection)
        if args.reconciled_projection is not None
        else None
    )
    if (
        not overlay_packets
        and identity_projection is None
        and not args.consolidate_only
    ):
        raise B3TemporalDecisionLedgerError(
            "at least one --overlay, --reconciled-projection, or "
            "--consolidate-only is required"
        )

    reconciled, ledger, report = fold_b3_temporal_review_overlays_v1(
        chapter_artifact=chapter_artifact,
        overlay_packets=overlay_packets,
        reconciled_identity_projection=identity_projection,
        frame_catalog=frame_catalog,
        identity_component_catalogs=identity_component_catalogs,
        consolidation_only=args.consolidate_only,
    )
    args.out_dir.mkdir(parents=True, exist_ok=False)
    _write(args.out_dir / "reconciled_temporal_artifact.json", reconciled)
    _write(args.out_dir / "temporal_decision_ledger.json", ledger)
    _write(args.out_dir / "apply_report.json", report)
    _write(args.out_dir / "frame_catalog.json", reconciled["frame_catalog"])
    print(
        "B3 temporal reviews applied: "
        f"overlays={report['overlay_count']} "
        f"states={report['before']['effective_states']}->"
        f"{report['after']['effective_states']} "
        f"pending={report['before']['pending_cases']}->"
        f"{report['after']['pending_cases']} "
        f"resolved={report['before']['resolved_cases']}->"
        f"{report['after']['resolved_cases']} "
        f"identity_superseded="
        f"{len(report.get('superseded_inherited_identity_case_ids') or [])} "
        "provider_calls=0"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (B3TemporalDecisionLedgerError, B3TemporalPrefixError) as exc:
        raise SystemExit(f"B3 temporal apply refused the input: {exc}") from exc
