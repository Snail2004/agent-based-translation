from __future__ import annotations

import argparse
import json

from pipeline.ingest.source_package_exporter import export_source_package


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct deterministic HTML and Markdown from a Canonical "
            "Source Package and a block-keyed translation overlay"
        )
    )
    parser.add_argument("--package", required=True, help="Canonical source package")
    parser.add_argument("--translations", required=True, help="Translation overlay JSON")
    parser.add_argument("--output-dir", required=True, help="New output directory")
    parser.add_argument(
        "--pandoc-executable",
        default="pandoc",
        help=(
            "Local Pandoc executable used to render preserved TeX as offline "
            "MathML; unavailable or unsupported equations remain visible as TeX"
        ),
    )
    parser.add_argument(
        "--review-mode",
        choices=("error", "markers"),
        default="error",
        help="Fail on unresolved rows, or render visible review markers",
    )
    args = parser.parse_args()

    result = export_source_package(
        args.package,
        args.translations,
        args.output_dir,
        review_mode=args.review_mode,
        pandoc_executable=args.pandoc_executable,
    )
    print(json.dumps(result.manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
