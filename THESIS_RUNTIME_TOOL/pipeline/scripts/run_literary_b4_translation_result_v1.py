from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from pipeline.literary.b4_translation_result_v1 import (
    write_translation_result_v1,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a Literary B4 translation result without runtime history"
    )
    parser.add_argument("--translation", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        translation = json.loads(
            args.translation.read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"cannot load translation artifact: {args.translation}") from exc
    if not isinstance(translation, dict):
        raise SystemExit("translation artifact must be a JSON object")
    report = write_translation_result_v1(
        translation_artifact=translation,
        out_dir=args.out_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
