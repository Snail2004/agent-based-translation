"""Apply a typed human relation correction offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from typing import Any, Mapping

from pipeline.literary.chapter_loop_observability_v1 import (
    ChapterLoopObservabilityError,
    LiteraryChapterLoopHistoryV1,
)
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.literary.relation_correction_overlay_v1 import (
    LiteraryRelationCorrectionError,
    apply_relation_correction_overlay_v1,
    verify_relation_correction_bundle_v1,
    verify_relation_correction_receipt_v1,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--component-root", type=Path)
    parser.add_argument("--chapter-ordinal", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.component_root is None) != (args.chapter_ordinal is None):
        raise LiteraryRelationCorrectionError(
            "component root and chapter ordinal must be supplied together"
        )
    if args.chapter_ordinal is not None and args.chapter_ordinal < 1:
        raise LiteraryRelationCorrectionError("chapter ordinal must be positive")
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
            / "relation_correction"
        )
        if args.out_dir.resolve() != expected:
            raise LiteraryRelationCorrectionError(
                "component correction output path is not deterministic"
            )
    if args.out_dir.exists():
        raise LiteraryRelationCorrectionError(
            f"out dir already exists (immutable artifacts): {args.out_dir}"
        )
    registry = _read_object(args.registry, "chapter registry")
    overlay = _read_object(args.overlay, "relation correction overlay")
    document = load_literary_source_document_v1(args.document)
    chapter_id = registry.get("chapter_id")
    chapters = [
        row
        for row in document.get("chapters") or []
        if isinstance(row, Mapping) and row.get("chapter_id") == chapter_id
    ]
    if len(chapters) != 1:
        raise LiteraryRelationCorrectionError(
            "document does not contain exactly one matching chapter"
        )
    corrected, normalized, receipt = apply_relation_correction_overlay_v1(
        chapter_registry=registry,
        chapter=chapters[0],
        overlay=overlay,
    )
    verify_relation_correction_receipt_v1(receipt)
    args.out_dir.mkdir(parents=True, exist_ok=False)
    _write(args.out_dir / "chapter_registry.json", corrected)
    _write(
        args.out_dir / "prior_cards.json",
        corrected["prior_cards_projection"],
    )
    _write(args.out_dir / "relation_correction_overlay.json", normalized)
    _write(args.out_dir / "relation_correction_receipt.json", receipt)
    _copy_registry_passthroughs(
        source_root=args.registry.resolve().parent,
        output_root=args.out_dir.resolve(),
    )
    verify_relation_correction_bundle_v1(
        source_registry=registry,
        corrected_registry=corrected,
        prior_cards=corrected["prior_cards_projection"],
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
            workflow_run_id = manifest.get("workflow_run_id")
            if not isinstance(workflow_run_id, str) or not workflow_run_id:
                raise LiteraryRelationCorrectionError(
                    "component manifest has no workflow run id"
                )
            history = LiteraryChapterLoopHistoryV1(
                run_root=component_root,
                run_id=workflow_run_id,
            )
            history.record_stage_revision(
                stage_id=stage_id,
                stage_name="b1_registry_writer",
                chapter_id=corrected["chapter_id"],
                revision_name="relation_correction",
                revision_root=args.out_dir,
                output_names=(
                    "chapter_registry.json",
                    "prior_cards.json",
                    "relation_correction_overlay.json",
                    "relation_correction_receipt.json",
                ),
                parent_artifact_refs=(
                    f"literary/{stage_id}/chapter_registry.json",
                ),
                revision_metadata={
                    "effective_revision": True,
                    "source_registry_hash": receipt["source_registry_hash"],
                    "corrected_registry_hash": receipt[
                        "corrected_registry_hash"
                    ],
                    "human_semantic_correction_performed": True,
                    "provider_calls": 0,
                },
            )
        except ChapterLoopObservabilityError as exc:
            raise LiteraryRelationCorrectionError(
                f"cannot register relation correction in replay: {exc}"
            ) from exc
        replay_registered = True
    print(
        json.dumps(
            {
                "status": "relation_correction_applied",
                "chapter_id": corrected["chapter_id"],
                "source_registry_hash": receipt["source_registry_hash"],
                "corrected_registry_hash": receipt[
                    "corrected_registry_hash"
                ],
                "correction_count": len(receipt["correction_ids"]),
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


def _copy_registry_passthroughs(
    *, source_root: Path, output_root: Path
) -> None:
    for name in ("cross_chapter_hearing_queue.json", "writer_report.json"):
        source = source_root / name
        if source.is_file():
            shutil.copyfile(source, output_root / name)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryRelationCorrectionError(
            f"cannot read {label}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise LiteraryRelationCorrectionError(
            f"{label} must be a JSON object"
        )
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LiteraryRelationCorrectionError as exc:
        raise SystemExit(f"relation correction refused: {exc}") from exc
