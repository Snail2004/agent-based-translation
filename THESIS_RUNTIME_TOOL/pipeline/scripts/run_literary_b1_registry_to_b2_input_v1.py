from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_registry_to_b2_input_v1 import (
    build_b2_registry_input_package_v1,
    write_b2_registry_input_package_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = RUNTIME_ROOT.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project audited B1 chapter registries into immutable B2 input"
    )
    parser.add_argument("--registry-root", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--epub", type=Path, default=DEFAULT_EPUB)
    parser.add_argument(
        "--document",
        type=Path,
        help="sealed project document.json; when supplied, EPUB parsing is bypassed",
    )
    parser.add_argument(
        "--reconciled-projection",
        type=Path,
        help=(
            "reconciled_projection.json produced after cross-chapter decisions; "
            "omit only before the first hearing has been answered"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    document = (
        load_literary_source_document_v1(args.document)
        if args.document is not None
        else _load_document("wuthering_heights", Path(args.epub))[0]
    )
    if not any(document.get(key) for key in ("document_id", "id", "title")):
        document = {
            **document,
            "document_id": str(document.get("doc_id") or "literary_document"),
        }
    registries = [
        _read_object(Path(root) / "chapter_registry.json", "chapter registry")
        for root in args.registry_root
    ]
    reconciled_projection = (
        _read_object(args.reconciled_projection, "reconciled projection")
        if args.reconciled_projection is not None
        else None
    )
    package = build_b2_registry_input_package_v1(
        document=document,
        chapter_registries=registries,
        current_git_head=_git_head(),
        reconciled_projection=reconciled_projection,
    )
    target = write_b2_registry_input_package_v1(
        output_root=args.output_root,
        package=package,
    )
    report_body = {
        "schema_version": "literary_b1_registry_to_b2_report_v1",
        "status": "b2_input_sealed",
        "package_path": str(target),
        "package_hash": package["package_hash"],
        "source_document_sha256": package["source_document_sha256"],
        "ordered_chapter_ids": list(package["ordered_chapter_ids"]),
        "reconciled_projection_hash": (
            reconciled_projection.get("projection_hash")
            if reconciled_projection is not None
            else None
        ),
        "identity_merge_performed": False,
        "semantic_claim_inference_performed": False,
        "provider_called": False,
        "database_mutation_performed": False,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    (Path(args.output_root) / "adapter_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot read {label}: {path}") from exc
    if not isinstance(value, Mapping):
        raise SystemExit(f"{label} must be a JSON object")
    return dict(value)


def _git_head() -> str:
    value = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not value:
        raise SystemExit("cannot determine current Git HEAD")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
