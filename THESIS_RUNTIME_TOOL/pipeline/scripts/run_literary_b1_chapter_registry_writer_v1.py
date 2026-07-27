from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    build_b1_cross_chapter_hearing_queue_v1,
    seal_b1_chapter_registry_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seal a B1 Scan/Enrich/Local-Audit chapter registry (0 API)"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scan-artifact", type=Path, required=True)
    parser.add_argument("--enrich-artifact", type=Path, required=True)
    parser.add_argument("--audit-artifact", type=Path, required=True)
    parser.add_argument("--chapter", default="wh_ch01")
    parser.add_argument("--epub", type=Path, default=DEFAULT_EPUB)
    parser.add_argument(
        "--document",
        type=Path,
        help="sealed project document.json; when supplied, EPUB parsing is bypassed",
    )
    parser.add_argument(
        "--prior-cards",
        type=Path,
        help=(
            "the same prior_cards.json B1-Scan consumed; required for roster "
            "recognition proposals to reach a hearing, since the proposal names "
            "a prior card the current registry does not contain"
        ),
    )
    parser.add_argument(
        "--reconciled-projection",
        type=Path,
        help=(
            "latest reconciled_projection.json; when supplied, settled pairs "
            "are gated before a new cross-chapter hearing is queued"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_writer(
        output_dir=args.output_dir,
        scan_artifact_path=args.scan_artifact,
        enrich_artifact_path=args.enrich_artifact,
        audit_artifact_path=args.audit_artifact,
        chapter_id=args.chapter,
        epub_path=args.epub,
        document_path=args.document,
        prior_cards_path=args.prior_cards,
        reconciled_projection_path=args.reconciled_projection,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_writer(
    *,
    output_dir: Path,
    scan_artifact_path: Path,
    enrich_artifact_path: Path,
    audit_artifact_path: Path,
    chapter_id: str,
    epub_path: Path,
    document_path: Path | None = None,
    prior_cards_path: Path | None = None,
    reconciled_projection_path: Path | None = None,
) -> dict[str, Any]:
    output = Path(output_dir)
    if output.exists():
        raise SystemExit("output directory already exists")
    prior_cards = None
    if prior_cards_path is not None:
        loaded = _read_json_value(prior_cards_path, "prior cards")
        # Accept either the bare list or the registry's projection wrapper, so
        # the previous chapter's own output can be handed straight back in.
        if isinstance(loaded, dict):
            loaded = loaded.get("cards")
        if not isinstance(loaded, list):
            raise SystemExit("prior cards file must hold a list of cards")
        prior_cards = loaded
    scan = _read_json(scan_artifact_path, "B1-Scan artifact")
    enrich = _read_json(enrich_artifact_path, "B1-Enrich artifact")
    audit = _read_json(audit_artifact_path, "Local Auditor artifact")
    reconciled_projection = (
        _read_json(reconciled_projection_path, "reconciled projection")
        if reconciled_projection_path is not None
        else None
    )
    document = (
        load_literary_source_document_v1(document_path)
        if document_path is not None
        else _load_document("wuthering_heights", Path(epub_path))[0]
    )
    try:
        chapter = next(
            row for row in document["chapters"] if row["chapter_id"] == chapter_id
        )
    except StopIteration as exc:
        raise SystemExit(f"chapter is absent: {chapter_id}") from exc

    registry = seal_b1_chapter_registry_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
    )
    hearing_queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=audit,
        prior_cards=prior_cards,
        reconciled_projection=reconciled_projection,
    )
    ready_hearings = [
        row
        for row in hearing_queue["components"]
        if row["lifecycle_state"] == "ready_for_hearing"
    ]
    metrics = registry["metrics"]
    report_body = {
        "schema_version": "literary_b1_chapter_registry_writer_report_v1",
        "status": "chapter_registry_sealed",
        "chapter_id": chapter_id,
        "registry_hash": registry["registry_hash"],
        "metrics": metrics,
        "cross_chapter_hearing_count": len(hearing_queue["components"]),
        "ready_cross_chapter_hearing_count": len(ready_hearings),
        "cross_chapter_counts_by_route": hearing_queue["metrics"][
            "counts_by_route"
        ],
        "waiting_cross_chapter_hearing_count": hearing_queue["metrics"][
            "waiting_count"
        ],
        "chapter_loop_complete": not ready_hearings,
        "provider_called": False,
        "identity_authority_granted": False,
        "book_authority_granted": False,
        "database_mutation_performed": False,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "chapter_registry.json", registry)
    _write_json(output / "cross_chapter_hearing_queue.json", hearing_queue)
    _write_json(
        output / "prior_cards.json",
        registry["prior_cards_projection"],
    )
    _write_json(output / "writer_report.json", report)
    return report


def _read_json(path: Path, label: str) -> dict[str, Any]:
    value = _read_json_value(path, label)
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return value


def _read_json_value(path: Path, label: str) -> Any:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot read {label}") from exc
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
