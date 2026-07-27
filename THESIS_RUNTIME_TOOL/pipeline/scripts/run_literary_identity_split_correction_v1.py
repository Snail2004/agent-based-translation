"""Rebuild one B1 registry after an explicit identity-merge retraction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    build_b1_cross_chapter_hearing_queue_v1,
    seal_b1_chapter_registry_v1,
)
from pipeline.literary.chapter_loop_observability_v1 import (
    ChapterLoopObservabilityError,
    LiteraryChapterLoopHistoryV1,
)
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.identity_split_correction_v1 import (
    LiteraryIdentitySplitCorrectionError,
    apply_identity_split_to_local_audit_v1,
    attach_identity_split_lineage_v1,
    build_identity_split_receipt_v1,
    verify_identity_split_bundle_v1,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--scan-artifact", type=Path, required=True)
    parser.add_argument("--enrich-artifact", type=Path, required=True)
    parser.add_argument("--audit-artifact", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--prior-cards", type=Path)
    parser.add_argument("--reconciled-projection", type=Path)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--component-root", type=Path)
    parser.add_argument("--chapter-ordinal", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.component_root is None) != (args.chapter_ordinal is None):
        raise LiteraryIdentitySplitCorrectionError(
            "component root and chapter ordinal must be supplied together"
        )
    component_root = (
        args.component_root.resolve()
        if args.component_root is not None
        else None
    )
    if component_root is not None:
        expected = (
            component_root
            / "corrections"
            / "chapters"
            / f"ch{args.chapter_ordinal:03d}"
            / "identity_split_correction"
        )
        if args.out_dir.resolve() != expected:
            raise LiteraryIdentitySplitCorrectionError(
                "component correction output path is not deterministic"
            )
    if args.out_dir.exists():
        raise LiteraryIdentitySplitCorrectionError(
            f"out dir already exists (immutable artifacts): {args.out_dir}"
        )

    source_registry = _read_object(args.registry, "source registry")
    scan = _read_object(args.scan_artifact, "B1 Scan artifact")
    enrich = _read_object(args.enrich_artifact, "B1 Enrich artifact")
    source_audit = _read_object(args.audit_artifact, "Local Auditor artifact")
    overlay = _read_object(args.overlay, "identity split overlay")
    corrected_audit, normalized = apply_identity_split_to_local_audit_v1(
        source_registry=source_registry,
        source_local_audit=source_audit,
        overlay=overlay,
    )
    document = load_literary_source_document_v1(args.document)
    chapters = [
        row
        for row in document.get("chapters") or []
        if isinstance(row, Mapping)
        and row.get("chapter_id") == source_registry.get("chapter_id")
    ]
    if len(chapters) != 1:
        raise LiteraryIdentitySplitCorrectionError(
            "document does not contain exactly one matching chapter"
        )
    prior_cards = _read_cards(args.prior_cards)
    projection = (
        _read_object(args.reconciled_projection, "reconciled projection")
        if args.reconciled_projection is not None
        else None
    )
    corrected_registry = seal_b1_chapter_registry_v1(
        chapter=chapters[0],
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=corrected_audit,
    )
    corrected_registry = attach_identity_split_lineage_v1(
        corrected_registry=corrected_registry,
        normalized_overlay=normalized,
    )
    queue = build_b1_cross_chapter_hearing_queue_v1(
        chapter_registry=corrected_registry,
        scan_artifact=scan,
        enrich_artifact=enrich,
        audit_artifact=corrected_audit,
        prior_cards=prior_cards,
        reconciled_projection=projection,
    )
    ready = [
        row
        for row in queue["components"]
        if row["lifecycle_state"] == "ready_for_hearing"
    ]
    report_body = {
        "schema_version": "literary_b1_chapter_registry_writer_report_v1",
        "status": "chapter_registry_sealed",
        "chapter_id": corrected_registry["chapter_id"],
        "registry_hash": corrected_registry["registry_hash"],
        "metrics": corrected_registry["metrics"],
        "cross_chapter_hearing_count": len(queue["components"]),
        "ready_cross_chapter_hearing_count": len(ready),
        "cross_chapter_counts_by_route": queue["metrics"]["counts_by_route"],
        "waiting_cross_chapter_hearing_count": queue["metrics"]["waiting_count"],
        "chapter_loop_complete": not ready,
        "provider_called": False,
        "identity_authority_granted": False,
        "book_authority_granted": False,
        "database_mutation_performed": False,
        "production_publish_performed": False,
    }
    writer_report = {
        **report_body,
        "report_hash": canonical_hash(report_body),
    }
    receipt = build_identity_split_receipt_v1(
        source_registry=source_registry,
        corrected_registry=corrected_registry,
        source_local_audit=source_audit,
        corrected_local_audit=corrected_audit,
        normalized_overlay=normalized,
        queue_hash=queue["queue_hash"],
    )

    args.out_dir.mkdir(parents=True, exist_ok=False)
    _write(args.out_dir / "chapter_registry.json", corrected_registry)
    _write(
        args.out_dir / "prior_cards.json",
        corrected_registry["prior_cards_projection"],
    )
    _write(args.out_dir / "cross_chapter_hearing_queue.json", queue)
    _write(args.out_dir / "writer_report.json", writer_report)
    _write(
        args.out_dir / "corrected_local_audit_artifact.json",
        corrected_audit,
    )
    _write(args.out_dir / "identity_split_correction_overlay.json", normalized)
    _write(args.out_dir / "identity_split_correction_receipt.json", receipt)
    verify_identity_split_bundle_v1(
        source_registry=source_registry,
        corrected_registry=corrected_registry,
        corrected_local_audit=corrected_audit,
        prior_cards=corrected_registry["prior_cards_projection"],
        queue=queue,
        writer_report=writer_report,
        normalized_overlay=normalized,
        receipt=receipt,
    )

    replay_registered = False
    if component_root is not None:
        stage_id = f"ch{args.chapter_ordinal:03d}_b1_registry_writer"
        try:
            manifest = _read_object(
                component_root / "component_manifest.json",
                "component manifest",
            )
            history = LiteraryChapterLoopHistoryV1(
                run_root=component_root,
                run_id=str(manifest["workflow_run_id"]),
            )
            history.record_stage_revision(
                stage_id=stage_id,
                stage_name="b1_registry_writer",
                chapter_id=corrected_registry["chapter_id"],
                revision_name="identity_split_correction",
                revision_root=args.out_dir,
                output_names=(
                    "chapter_registry.json",
                    "prior_cards.json",
                    "cross_chapter_hearing_queue.json",
                    "writer_report.json",
                    "corrected_local_audit_artifact.json",
                    "identity_split_correction_overlay.json",
                    "identity_split_correction_receipt.json",
                ),
                parent_artifact_refs=(
                    f"literary/{stage_id}/chapter_registry.json",
                ),
                revision_metadata={
                    "effective_revision": True,
                    "requires_downstream_rebuild": True,
                    "source_registry_hash": receipt["source_registry_hash"],
                    "corrected_registry_hash": receipt[
                        "corrected_registry_hash"
                    ],
                    "human_semantic_correction_performed": True,
                    "provider_calls": 0,
                },
            )
        except ChapterLoopObservabilityError as exc:
            raise LiteraryIdentitySplitCorrectionError(
                f"cannot register identity split in replay: {exc}"
            ) from exc
        replay_registered = True
    print(
        json.dumps(
            {
                "status": "identity_split_correction_applied",
                "chapter_id": corrected_registry["chapter_id"],
                "source_registry_hash": receipt["source_registry_hash"],
                "corrected_registry_hash": receipt["corrected_registry_hash"],
                "split_mappings": receipt["split_mappings"],
                "provider_calls": 0,
                "replay_registered": replay_registered,
                "production_publish_performed": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _read_cards(path: Path | None) -> list[Mapping[str, Any]] | None:
    if path is None:
        return None
    value = _read_value(path, "prior cards")
    if isinstance(value, Mapping):
        value = value.get("cards")
    if not isinstance(value, list) or not all(
        isinstance(row, Mapping) for row in value
    ):
        raise LiteraryIdentitySplitCorrectionError(
            "prior cards must be a list of objects"
        )
    return list(value)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = _read_value(path, label)
    if not isinstance(value, dict):
        raise LiteraryIdentitySplitCorrectionError(
            f"{label} must be an object"
        )
    return value


def _read_value(path: Path, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryIdentitySplitCorrectionError(
            f"cannot read {label}: {path}"
        ) from exc


def _write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LiteraryIdentitySplitCorrectionError as exc:
        raise SystemExit(f"identity split correction refused: {exc}") from exc
