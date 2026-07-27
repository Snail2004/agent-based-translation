"""Correct one cross-chapter identity decision and reproject it offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    build_projected_prior_cards_v1,
    project_reconciled_b1_registry_v1,
)
from pipeline.literary.chapter_loop_bindings_v1 import load_stage_bindings_v1
from pipeline.literary.chapter_loop_current_executor_v1 import (
    LiteraryChapterLoopExecutorError,
    write_effective_stage_roots_manifest_v1,
)
from pipeline.literary.chapter_loop_observability_v1 import (
    ChapterLoopObservabilityError,
    LiteraryChapterLoopHistoryV1,
)
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.literary.identity_decision_correction_v1 import (
    LiteraryIdentityDecisionCorrectionError,
    apply_identity_decision_correction_v1,
    verify_identity_decision_correction_bundle_v1,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--registry", action="append", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--component-root", type=Path)
    parser.add_argument("--chapter-ordinal", type=int)
    parser.add_argument("--code-revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.component_root is None) != (args.chapter_ordinal is None):
        raise LiteraryIdentityDecisionCorrectionError(
            "component root and chapter ordinal must be supplied together"
        )
    if args.chapter_ordinal is not None and args.chapter_ordinal < 1:
        raise LiteraryIdentityDecisionCorrectionError(
            "chapter ordinal must be positive"
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
            / "identity_decision_correction"
        )
        if args.out_dir.resolve() != expected:
            raise LiteraryIdentityDecisionCorrectionError(
                "component correction output path is not deterministic"
            )
    if args.out_dir.exists():
        raise LiteraryIdentityDecisionCorrectionError(
            f"out dir already exists (immutable artifacts): {args.out_dir}"
        )

    source_ledger = _read_object(args.ledger, "decision ledger")
    source_document = load_literary_source_document_v1(args.document)
    overlay = _read_object(args.overlay, "identity decision correction overlay")
    registries = [
        _read_object(path, f"source registry {index}")
        for index, path in enumerate(args.registry)
    ]
    for registry in registries:
        verify_b1_chapter_registry_v1(registry)

    corrected, normalized, receipt = apply_identity_decision_correction_v1(
        decision_ledger=source_ledger,
        source_document=source_document,
        overlay=overlay,
    )
    projection = project_reconciled_b1_registry_v1(
        registries=registries,
        ledger=corrected,
    )
    prior_cards = build_projected_prior_cards_v1(
        registries=registries,
        projection=projection,
    )

    args.out_dir.mkdir(parents=True, exist_ok=False)
    for index, registry in enumerate(registries):
        _write(args.out_dir / f"source_registry_{index:02d}.json", registry)
    _write(args.out_dir / "decision_ledger.json", corrected)
    _write(args.out_dir / "reconciled_projection.json", projection)
    _write(args.out_dir / "prior_cards.json", prior_cards)
    _write(
        args.out_dir / "identity_decision_correction_overlay.json",
        normalized,
    )
    _write(
        args.out_dir / "identity_decision_correction_receipt.json",
        receipt,
    )
    report = {
        "schema_version": "literary_b1_apply_decisions_report_v1",
        "book_id": corrected["book_id"],
        "decisions_appended": 0,
        "ledger_entry_count": len(corrected["entries"]),
        "ledger_hash": corrected["ledger_hash"],
        "projection_hash": projection["projection_hash"],
        "prior_card_count": len(prior_cards),
        "source_registry_hashes": projection["source_registry_hashes"],
        "metrics": projection["metrics"],
        "provider_calls": 0,
        "identity_authority_granted": False,
        "human_semantic_correction_performed": True,
        "identity_decision_correction_id": normalized["correction_id"],
        "source_ledger_hash": source_ledger["ledger_hash"],
    }
    _write(args.out_dir / "apply_report.json", report)
    verify_identity_decision_correction_bundle_v1(
        source_ledger=source_ledger,
        source_document=source_document,
        registries=registries,
        corrected_ledger=corrected,
        reconciled_projection=projection,
        prior_cards=prior_cards,
        normalized_overlay=normalized,
        receipt=receipt,
    )

    replay_registered = False
    effective_root_bound = False
    if component_root is not None:
        stage_id = f"ch{args.chapter_ordinal:03d}_identity_apply"
        manifest = _read_object(
            component_root / "component_manifest.json",
            "component manifest",
        )
        workflow_run_id = _required_string(
            manifest.get("workflow_run_id"),
            "component workflow run id",
        )
        history = LiteraryChapterLoopHistoryV1(
            run_root=component_root,
            run_id=workflow_run_id,
        )
        if args.code_revision is not None:
            revisions = list(manifest.get("code_revision_history") or [])
            if not revisions:
                revisions = [_required_string(manifest.get("code_revision"), "code revision")]
            if args.code_revision not in revisions:
                revisions.append(args.code_revision)
            history.synchronize_code_revisions(revisions)
        chapter_id = _required_string(
            corrected["entries"][
                next(
                    index
                    for index, row in enumerate(corrected["entries"])
                    if row["entry_id"] == receipt["new_entry_id"]
                )
            ].get("chapter_id"),
            "corrected decision chapter_id",
        )
        history.record_stage_revision(
            stage_id=stage_id,
            stage_name="identity_apply",
            chapter_id=chapter_id,
            revision_name="identity_decision_correction",
            revision_root=args.out_dir,
            output_names=(
                "decision_ledger.json",
                "reconciled_projection.json",
                "prior_cards.json",
                "apply_report.json",
                "identity_decision_correction_overlay.json",
                "identity_decision_correction_receipt.json",
            ),
            parent_artifact_refs=(
                f"literary/{stage_id}/decision_ledger.json",
                f"literary/{stage_id}/reconciled_projection.json",
            ),
            revision_metadata={
                "effective_revision": True,
                "source_ledger_hash": receipt["source_ledger_hash"],
                "corrected_ledger_hash": receipt["corrected_ledger_hash"],
                "human_semantic_correction_performed": True,
                "provider_calls": 0,
            },
        )
        session = _read_object(
            component_root / "chapter_loop_session.json",
            "chapter loop session",
        )
        stage_binding_path = Path(
            _required_string(
                session.get("stage_binding_path"),
                "stage binding path",
            )
        )
        plan = _read_object(component_root / "run_plan.json", "run plan")
        write_effective_stage_roots_manifest_v1(
            run_root=component_root,
            plan=plan,
            stage_bindings=load_stage_bindings_v1(stage_binding_path),
            effective_roots={stage_id: args.out_dir},
        )
        replay_registered = True
        effective_root_bound = True

    print(
        json.dumps(
            {
                "status": "identity_decision_correction_applied",
                "old_entry_id": receipt["old_entry_id"],
                "new_entry_id": receipt["new_entry_id"],
                "old_verdict": receipt["old_verdict"],
                "new_verdict": receipt["new_verdict"],
                "source_ledger_hash": receipt["source_ledger_hash"],
                "corrected_ledger_hash": receipt["corrected_ledger_hash"],
                "projection_hash": projection["projection_hash"],
                "metrics": projection["metrics"],
                "provider_calls": 0,
                "replay_registered": replay_registered,
                "effective_root_bound": effective_root_bound,
                "production_publish_performed": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise LiteraryIdentityDecisionCorrectionError(
            f"cannot read {label}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise LiteraryIdentityDecisionCorrectionError(
            f"{label} must be a JSON object"
        )
    return value


def _write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LiteraryIdentityDecisionCorrectionError(
            f"{label} must be a non-empty string"
        )
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ChapterLoopObservabilityError,
        LiteraryChapterLoopExecutorError,
        LiteraryIdentityDecisionCorrectionError,
    ) as exc:
        raise SystemExit(f"identity decision correction refused: {exc}") from exc
