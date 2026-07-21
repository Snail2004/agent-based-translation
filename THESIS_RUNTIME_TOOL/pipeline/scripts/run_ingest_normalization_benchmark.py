from __future__ import annotations

import argparse
from functools import partial
from pathlib import Path

from pipeline.ingest.normalization_adapters import run_app_current, run_docling, run_pandoc
from pipeline.ingest.normalization_benchmark import SourceSpec, benchmark_many, write_report


def _source_spec(value: str) -> SourceSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Source must be LABEL=PATH")
    label, raw_path = value.split("=", 1)
    path = Path(raw_path).expanduser().resolve()
    if not label.strip():
        raise argparse.ArgumentTypeError("Source label cannot be empty")
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"Source does not exist: {path}")
    return SourceSpec(label=label.strip(), path=path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark offline input normalization adapters")
    parser.add_argument("--source", action="append", type=_source_spec, required=True, help="LABEL=PATH")
    parser.add_argument(
        "--adapters",
        default="app_current,pandoc,docling",
        help="Comma-separated subset of app_current,pandoc,docling",
    )
    parser.add_argument("--pandoc", default="pandoc", help="Pandoc executable")
    parser.add_argument("--docling-python", help="Python executable containing docling")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--include-manifest",
        action="store_true",
        help="Include per-block hashes and structural refs in benchmark.json",
    )
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requested = [item.strip() for item in args.adapters.split(",") if item.strip()]
    known = {"app_current", "pandoc", "docling"}
    unknown = sorted(set(requested) - known)
    if unknown:
        raise SystemExit(f"Unknown adapters: {', '.join(unknown)}")
    runners = {}
    if "app_current" in requested:
        runners["app_current"] = run_app_current
    if "pandoc" in requested:
        runners["pandoc"] = partial(run_pandoc, executable=args.pandoc)
    if "docling" in requested:
        if not args.docling_python:
            raise SystemExit("--docling-python is required when docling is selected")
        runners["docling"] = partial(run_docling, python_executable=args.docling_python)
    report = benchmark_many(
        args.source,
        runners,
        repeat=args.repeat,
        include_manifest=args.include_manifest,
    )
    json_path, markdown_path = write_report(report, args.output_dir)
    print(json_path)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
