from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2] / "app" / "backend"


def parse_source(source: Path) -> dict[str, Any]:
    backend = _backend_root()
    sys.path.insert(0, str(backend))
    try:
        from config import PIPELINE_VERSION, SCHEMA_VERSION
        from services.extraction import split_epub, split_html, split_markdown, split_txt
    finally:
        sys.path.pop(0)

    report: dict[str, Any] = {}
    suffix = source.suffix.lower()
    if suffix == ".epub":
        chapters = split_epub(source, report)
    elif suffix in {".md", ".markdown"}:
        chapters = split_markdown(source.read_text(encoding="utf-8-sig", errors="replace"), report)
    elif suffix in {".html", ".htm"}:
        chapters = split_html(source.read_text(encoding="utf-8-sig", errors="replace"), report)
    elif suffix == ".txt":
        chapters = split_txt(source.read_text(encoding="utf-8", errors="replace"), report)
    else:
        raise ValueError(f"Unsupported source for app-current worker: {suffix}")
    return {
        "adapter_version": f"pipeline-{PIPELINE_VERSION}/schema-{SCHEMA_VERSION}",
        "chapters": chapters,
        "report": report,
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: normalization_app_worker.py SOURCE")
    payload = parse_source(Path(sys.argv[1]).resolve())
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
