from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.literary.b4_address_anchor_v1 import load_style_profile_v1
from pipeline.literary.b4_editorial_review_v1 import (
    apply_approved_editorial_reviews_v1,
    build_editorial_approval_v1,
    build_editorial_review_artifact_v1,
    build_editorial_review_packets_v1,
    render_editorial_review_request_v1,
    validate_editorial_review_response_v1,
)
from pipeline.literary.chapter_source_document_v1 import (
    chapter_from_document_v1,
    load_literary_source_document_v1,
)
from pipeline.literary.checkpoint import canonical_hash


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare, validate, approve, or apply B4 Editorial Review"
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--translation", type=Path, required=True)
    prepare.add_argument("--document", type=Path, required=True)
    prepare.add_argument("--translator-pack", type=Path, required=True)
    prepare.add_argument("--lint-report", type=Path, required=True)
    prepare.add_argument("--style-design", type=Path, required=True)
    prepare.add_argument("--style-profile-version", required=True)
    prepare.add_argument(
        "--selection-mode",
        choices=["all_blocks", "flagged_only", "flagged_plus_sample"],
        required=True,
    )
    prepare.add_argument("--explicit-block-id", action="append", default=[])
    prepare.add_argument("--sample-count", type=int, default=0)
    prepare.add_argument("--sample-seed", default="")
    prepare.add_argument("--context-radius", type=int, default=1)
    prepare.add_argument("--max-candidates-per-batch", type=int, default=8)
    prepare.add_argument("--window-slice", type=Path, action="append", default=[])
    prepare.add_argument("--out-dir", type=Path, required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--prepared-batch-root", type=Path, required=True)
    validate.add_argument("--response", type=Path, required=True)
    validate.add_argument("--provider-receipt", type=Path)
    validate.add_argument("--out-dir", type=Path, required=True)

    approval = commands.add_parser("build-approval")
    approval.add_argument("--translation", type=Path, required=True)
    approval.add_argument(
        "--review-artifact",
        type=Path,
        action="append",
        required=True,
    )
    approval.add_argument("--decision-file", type=Path, required=True)
    approval.add_argument("--out-dir", type=Path, required=True)

    apply = commands.add_parser("apply")
    apply.add_argument("--translation", type=Path, required=True)
    apply.add_argument(
        "--review-artifact",
        type=Path,
        action="append",
        required=True,
    )
    apply.add_argument("--approval", type=Path, required=True)
    apply.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare":
        report = _prepare(args)
    elif args.command == "validate":
        report = _validate(args)
    elif args.command == "build-approval":
        report = _build_approval(args)
    else:
        report = _apply(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    translation = _read(args.translation)
    document = load_literary_source_document_v1(args.document)
    chapter = chapter_from_document_v1(
        document,
        str(translation["chapter_id"]),
    )
    style_profile = load_style_profile_v1(
        design_doc=args.style_design,
        style_profile_version=args.style_profile_version,
    )
    packets, selection_report = build_editorial_review_packets_v1(
        translation_artifact=translation,
        chapter=chapter,
        translator_pack=_read(args.translator_pack),
        lint_report=_read(args.lint_report),
        style_profile_version=args.style_profile_version,
        style_profile_sha256=canonical_hash(style_profile),
        selection_mode=args.selection_mode,
        explicit_block_ids=args.explicit_block_id,
        sample_count=args.sample_count,
        sample_seed=args.sample_seed,
        context_radius=args.context_radius,
        max_candidates_per_batch=args.max_candidates_per_batch,
        window_slices=[_read(path) for path in args.window_slice],
    )
    output = _fresh(args.out_dir)
    _write(output / "editorial_selection_report.json", selection_report)
    index_rows = []
    total_estimate = 0
    for packet in packets:
        batch_root = output / f"batch_{int(packet['batch_index']):03d}"
        batch_root.mkdir()
        rendered = render_editorial_review_request_v1(
            review_packet=packet,
            style_profile=style_profile,
        )
        request = {
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": rendered.response_schema,
            "request_fingerprint": rendered.request_fingerprint,
        }
        estimate = estimate_prompt_tokens(
            rendered.messages,
            rendered.response_schema,
        )
        total_estimate += estimate
        _write(batch_root / "editorial_review_packet.json", packet)
        (batch_root / "style_profile.txt").write_text(
            style_profile,
            encoding="utf-8",
        )
        _write(batch_root / "request.json", request)
        batch_report = {
            "schema_version": "literary_b4_editorial_prepare_batch_report_v1",
            "status": "ready",
            "chapter_id": packet["chapter_id"],
            "batch_index": packet["batch_index"],
            "batch_count": packet["batch_count"],
            "candidate_block_count": len(packet["candidate_block_ids"]),
            "candidate_block_ids": packet["candidate_block_ids"],
            "request_fingerprint": rendered.request_fingerprint,
            "estimated_prompt_tokens": estimate,
            "provider_calls": 0,
        }
        _write(batch_root / "prepare_report.json", batch_report)
        index_rows.append(batch_report)
    report = {
        "schema_version": "literary_b4_editorial_prepare_report_v1",
        "status": selection_report["status"],
        "chapter_id": selection_report["chapter_id"],
        "candidate_block_count": selection_report["candidate_block_count"],
        "batch_count": len(packets),
        "estimated_prompt_tokens": total_estimate,
        "batches": index_rows,
        "provider_calls": 0,
        "artifact_hash": selection_report["artifact_hash"],
    }
    _write(output / "prepare_report.json", report)
    return report


def _validate(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.prepared_batch_root).resolve()
    packet = _read(root / "editorial_review_packet.json")
    style_profile = (root / "style_profile.txt").read_text(encoding="utf-8")
    rendered = render_editorial_review_request_v1(
        review_packet=packet,
        style_profile=style_profile,
    )
    prepared_request = _read(root / "request.json")
    if prepared_request.get("request_fingerprint") != (
        rendered.request_fingerprint
    ):
        raise SystemExit("prepared Editorial Review request differs")
    validated = validate_editorial_review_response_v1(
        rendered=rendered,
        response=_read(args.response),
    )
    receipt = _read(args.provider_receipt) if args.provider_receipt else None
    artifact = build_editorial_review_artifact_v1(
        rendered=rendered,
        validated_response=validated,
        provider_receipt=receipt,
        provider_called=receipt is not None,
    )
    output = _fresh(args.out_dir)
    _write(output / "validated_response.json", validated)
    _write(output / "editorial_review.json", artifact)
    report = {
        "schema_version": "literary_b4_editorial_validation_report_v1",
        "status": "complete",
        "chapter_id": artifact["chapter_id"],
        "batch_index": artifact["batch_index"],
        "candidate_block_count": len(artifact["candidate_block_ids"]),
        "action_counts": artifact["action_counts"],
        "severity_counts": artifact["severity_counts"],
        "provider_calls": 1 if receipt is not None else 0,
        "artifact_hash": artifact["artifact_hash"],
    }
    _write(output / "validation_report.json", report)
    return report


def _build_approval(args: argparse.Namespace) -> dict[str, Any]:
    translation = _read(args.translation)
    reviews = [_read(path) for path in args.review_artifact]
    decision_input = _read(args.decision_file)
    decisions = decision_input.get("decisions")
    if not isinstance(decisions, list):
        raise SystemExit("decision file must contain a decisions list")
    approval = build_editorial_approval_v1(
        source_translation_artifact_hash=str(translation["artifact_hash"]),
        review_artifact_hashes=[
            str(review["artifact_hash"]) for review in reviews
        ],
        decisions=decisions,
    )
    output = _fresh(args.out_dir)
    _write(output / "editorial_approval.json", approval)
    report = {
        "schema_version": "literary_b4_editorial_approval_report_v1",
        "status": "complete",
        "chapter_id": translation["chapter_id"],
        "decision_count": len(decisions),
        "provider_calls": 0,
        "artifact_hash": approval["artifact_hash"],
    }
    _write(output / "approval_report.json", report)
    return report


def _apply(args: argparse.Namespace) -> dict[str, Any]:
    edited, report = apply_approved_editorial_reviews_v1(
        translation_artifact=_read(args.translation),
        review_artifacts=[_read(path) for path in args.review_artifact],
        approval_artifact=_read(args.approval),
    )
    output = _fresh(args.out_dir)
    _write(
        output / f"translation_{edited['chapter_id']}_edited.json",
        edited,
    )
    _write(output / "editorial_apply_report.json", report)
    return report


def _fresh(path: Path) -> Path:
    root = Path(path).resolve()
    if root.exists():
        raise SystemExit(f"output directory already exists: {root}")
    root.mkdir(parents=True)
    return root


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot load JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise SystemExit(f"JSON object required: {path}")
    return dict(value)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
