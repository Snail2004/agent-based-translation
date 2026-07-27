from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_enrich_local_auditor_v1 import (
    build_b1_enrich_local_audit_manifest_v1,
    plan_b1_enrich_local_audit_batches_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DESIGN = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-render the exception-only B1-Enrich Local Auditor"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scan-artifact", type=Path, required=True)
    parser.add_argument("--enrich-artifact", type=Path, required=True)
    parser.add_argument("--chapter", default="wh_ch01")
    parser.add_argument("--epub", type=Path, default=DEFAULT_EPUB)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN)
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--max-input-tokens", type=int, default=20000)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_dry(
        output_dir=args.output_dir,
        scan_artifact_path=args.scan_artifact,
        enrich_artifact_path=args.enrich_artifact,
        chapter_id=args.chapter,
        epub_path=args.epub,
        design_doc=args.design_doc,
        model_id=args.model,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_dry(
    *,
    output_dir: Path,
    scan_artifact_path: Path,
    enrich_artifact_path: Path,
    chapter_id: str,
    epub_path: Path,
    design_doc: Path,
    model_id: str = "gpt-5.4",
    max_input_tokens: int = 20000,
    max_output_tokens: int = 4096,
) -> dict[str, Any]:
    output = Path(output_dir)
    if output.exists():
        raise SystemExit("output directory already exists")
    if max_output_tokens < 512 or max_output_tokens > 8192:
        raise SystemExit("max output tokens are outside the sealed dry range")
    if max_input_tokens < 1024:
        raise SystemExit("max input tokens are outside the sealed dry range")
    scan = _read_json(scan_artifact_path, "B1-Scan artifact")
    enrich = _read_json(enrich_artifact_path, "B1-Enrich artifact")
    document, _mapping = _load_document("wuthering_heights", Path(epub_path))
    try:
        chapter = next(
            row for row in document["chapters"] if row["chapter_id"] == chapter_id
        )
    except StopIteration as exc:
        raise SystemExit(f"chapter is absent: {chapter_id}") from exc
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
    )
    batch_plan, batches = plan_b1_enrich_local_audit_batches_v1(
        chapter=chapter,
        scan_artifact=scan,
        enrich_artifact=enrich,
        design_doc=design_doc,
        prompt_token_cap=max_input_tokens,
        output_token_cap=max_output_tokens,
        model_id=model_id,
    )
    component_kinds = sorted({row["component_kind"] for row in manifest["components"]})
    report_body = {
        "schema_version": "literary_b1_enrich_local_audit_dry_report_v2",
        "status": "dry_rendered_no_api",
        "chapter_id": chapter_id,
        "model_id": model_id,
        "manifest_hash": manifest["manifest_hash"],
        "request_fingerprint": batch_plan["batch_plan_hash"],
        "batch_plan_hash": batch_plan["batch_plan_hash"],
        "batch_count": len(batches),
        "batches": batch_plan["batches"],
        "component_count": len(manifest["components"]),
        "component_counts_by_kind": {
            kind: sum(
                1 for row in manifest["components"] if row["component_kind"] == kind
            )
            for kind in component_kinds
        },
        "entity_card_count": len(manifest["entity_cards"]),
        "source_block_count": len(manifest["source_blocks"]),
        "mechanical_noop_count": len(manifest["mechanical_noops"]),
        "quarantined_row_count": len(manifest["quarantined_rows"]),
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "provider_called": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "manifest.json", manifest)
    _write_json(output / "batch_plan.json", batch_plan)
    for index, batch in enumerate(batches, start=1):
        batch_dir = output / "batches" / f"{index:03d}_{batch.batch_id}"
        batch_dir.mkdir(parents=True, exist_ok=False)
        request = batch.rendered.to_dict()
        request["messages"] = [dict(row) for row in batch.rendered.messages]
        request["response_schema"] = batch.request["response_schema"]
        _write_json(batch_dir / "manifest.json", batch.manifest)
        _write_json(batch_dir / "request.json", request)
        _write_json(
            batch_dir / "token_preflight.json",
            batch.token_preflight.to_payload(),
        )
    _write_json(output / "dry_report.json", report)
    return report


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
