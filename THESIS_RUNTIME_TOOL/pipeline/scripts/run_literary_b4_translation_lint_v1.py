from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b4_translation_lint_v1 import (
    lint_translation_chapter_v1,
)
from pipeline.literary.chapter_source_document_v1 import (
    chapter_from_document_v1,
    load_literary_source_document_v1,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lint an assembled B4 translation without provider calls"
    )
    parser.add_argument("--translation", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--window-plan", type=Path, required=True)
    parser.add_argument("--translator-pack", type=Path)
    parser.add_argument("--mechanical-policy", type=Path)
    parser.add_argument("--apply-mechanical-fixes", action="store_true")
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    translation = _read(args.translation)
    document = load_literary_source_document_v1(args.document)
    chapter = chapter_from_document_v1(
        document, str(translation["chapter_id"])
    )
    report, corrected = lint_translation_chapter_v1(
        translation_artifact=translation,
        chapter=chapter,
        window_plan=_read(args.window_plan),
        translator_pack=(
            _read(args.translator_pack) if args.translator_pack else None
        ),
        mechanical_policy=(
            _read(args.mechanical_policy) if args.mechanical_policy else None
        ),
        apply_mechanical_fixes=args.apply_mechanical_fixes,
    )
    output = _fresh(args.out_dir)
    _write(output / "translation_lint_report.json", report)
    if corrected is not None:
        _write(output / "mechanically_corrected_translation.json", corrected)
    summary = {
        "status": report["status"],
        "chapter_id": report["chapter_id"],
        "issue_count": report["issue_count"],
        "issue_by_kind": report["issue_by_kind"],
        "observation_count": report["observation_count"],
        "observation_by_kind": report["observation_by_kind"],
        "mechanical_correction_count": report[
            "mechanical_correction_count"
        ],
        "remaining_issue_count": report["remaining_issue_count"],
        "provider_calls": 0,
        "artifact_hash": report["artifact_hash"],
    }
    _write(output / "lint_run_report.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


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
