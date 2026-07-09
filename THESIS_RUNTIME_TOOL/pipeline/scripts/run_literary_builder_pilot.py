from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from pipeline.literary.builder_pilot import (  # noqa: E402
    build_dry_run_artifacts,
    estimate_m2,
    estimate_m3,
    estimate_m1,
    build_window_manifest,
    load_great_gatsby_epub,
    load_wuthering_heights_epub,
    revalidate_m1_artifacts,
    run_m2,
    run_m3,
    run_m1,
    select_chapters,
    validate_builtin_fixtures,
)
from pipeline.agents.llm_client import LLMClient  # noqa: E402
from pipeline.agents.llm_config import load_llm_config  # noqa: E402


DEFAULT_EPUB = (
    REPO_ROOT
    / "reference"
    / "literary"
    / "wuthering_heights"
    / "en"
    / "wuthering_heights_gutenberg_768_epub3_images.epub"
)
DEFAULT_GATSBY_EPUB = (
    REPO_ROOT
    / "reference"
    / "literary"
    / "great_gatsby"
    / "en"
    / "great_gatsby_gutenberg_64317_epub3_images.epub"
)
DEFAULT_OUT = TOOL_ROOT / "data" / "reports" / "literary_l2a0_wh_builder_scaffold"
DEFAULT_L2A1_OUT = TOOL_ROOT / "data" / "reports" / "literary_l2a1_wh_builder_pilot"
DEFAULT_DESIGN = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="L2A-0 literary Builder scaffold: ingest WH, validate fixtures, and write dry-run artifacts. No API."
    )
    parser.add_argument(
        "--book-id",
        choices=["wuthering_heights", "great_gatsby"],
        default="wuthering_heights",
        help="Literary source loader to use.",
    )
    parser.add_argument("--epub", default=str(DEFAULT_EPUB), help="Pinned English EPUB path.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Artifact directory.")
    parser.add_argument(
        "--milestone",
        choices=["M1", "M2", "M3"],
        help="Run an L2A-1 milestone. M1 = B0+B1+B2; M2 = B3 digest; M3 = B4 consolidation.",
    )
    parser.add_argument(
        "--chapters",
        nargs="+",
        default=["1", "2", "3", "4"],
        help="Pilot chapters, e.g. 1 2 3 4 or wh_ch01.",
    )
    parser.add_argument(
        "--validate-fixtures",
        action="store_true",
        help="Run the three L2A-0 acceptance fixtures and exit nonzero if any fail.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write document.json, chapter mapping, window manifest, and dry-run report. No API.",
    )
    parser.add_argument(
        "--write-document",
        action="store_true",
        help="Write document.json and mapping even without --dry-run.",
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="For --milestone M1/M2: estimate prompt tokens/cost without API.",
    )
    parser.add_argument(
        "--revalidate-existing",
        action="store_true",
        help="For --milestone M1: re-run validators on existing artifacts without API.",
    )
    parser.add_argument(
        "--confirm-usd",
        type=float,
        help="For --milestone M1/M2 real run: refuse if estimated cap exceeds this amount.",
    )
    parser.add_argument(
        "--config",
        default=str(TOOL_ROOT / "pipeline" / "configs" / "llm_prepass.yaml"),
        help="LLM config for L2A-1 milestones.",
    )
    parser.add_argument(
        "--cache",
        default=str(TOOL_ROOT / "data" / "jobs" / "literary_builder_cache.sqlite3"),
        help="LLM cache SQLite path for L2A-1 milestones.",
    )
    parser.add_argument(
        "--design-doc",
        default=str(DEFAULT_DESIGN),
        help="Prompt/schema design doc. Prompts are extracted verbatim.",
    )
    parser.add_argument(
        "--m1-dir",
        help="For --milestone M2: directory containing the passed M1 artifacts.",
    )
    parser.add_argument(
        "--window-target-tokens",
        type=int,
        default=500,
        help="M1 active window target token estimate. Default preserves the 500-token baseline.",
    )
    parser.add_argument(
        "--window-max-blocks",
        type=int,
        default=8,
        help="M1 active window max block count. Default preserves the 8-block baseline.",
    )
    args = parser.parse_args()

    if args.milestone:
        return _run_milestone(args)

    reports = validate_builtin_fixtures()
    if args.validate_fixtures and any(not report.ok for report in reports):
        print(
            json.dumps(
                {"fixture_validation": [report.to_dict() for report in reports]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    if not args.dry_run and not args.write_document and args.validate_fixtures:
        print(
            json.dumps(
                {"fixture_validation": [report.to_dict() for report in reports]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.dry_run and not args.write_document:
        raise SystemExit("Pass --validate-fixtures, --write-document, or --dry-run")

    epub_path = _epub_path_for_args(args)
    document, mapping = _load_document(args.book_id, epub_path)
    selected = select_chapters(document, args.chapters)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    document_path = out_dir / "document.json"
    mapping_path = out_dir / "chapter_mapping.json"
    windows_path = out_dir / "window_manifest.json"
    fixtures_path = out_dir / "fixture_validation.json"

    document_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    mapping_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    windows = build_window_manifest(
        selected,
        window_target_tokens=args.window_target_tokens,
        window_max_blocks=args.window_max_blocks,
    )
    windows_path.write_text(
        json.dumps(windows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fixtures_path.write_text(
        json.dumps([report.to_dict() for report in reports], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    dry_run_path = None
    if args.dry_run:
        dry_run = build_dry_run_artifacts(document, args.chapters)
        dry_run_path = out_dir / "dry_run_report.json"
        dry_run_path.write_text(
            json.dumps(dry_run, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    summary = {
        "phase": "L2A-0",
        "zero_api": True,
        "epub": str(epub_path),
        "chapters_total": len(document.get("chapters") or []),
        "chapters_selected": [chapter["chapter_id"] for chapter in selected],
        "blocks_selected": sum(len(chapter.get("blocks") or []) for chapter in selected),
        "windows_selected": len(windows),
        "window_config": {
            "target_tokens": args.window_target_tokens,
            "max_blocks": args.window_max_blocks,
        },
        "fixture_pass": all(report.ok for report in reports),
        "outputs": {
            "document": str(document_path),
            "mapping": str(mapping_path),
            "windows": str(windows_path),
            "fixture_validation": str(fixtures_path),
            "dry_run": str(dry_run_path) if dry_run_path else None,
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _run_milestone(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    if str(out_dir) == str(DEFAULT_OUT):
        selected_slug = "_".join(str(chapter).replace("wh_", "") for chapter in args.chapters)
        out_dir = DEFAULT_L2A1_OUT / f"m1_{selected_slug}"
    m1_dir = Path(args.m1_dir) if args.m1_dir else out_dir
    epub_path = _epub_path_for_args(args)
    document, _mapping = _load_document(args.book_id, epub_path)
    config = load_llm_config(args.config)
    design_doc = Path(args.design_doc)

    if args.milestone == "M2":
        estimate = estimate_m2(
            document,
            args.chapters,
            design_doc=design_doc,
            config=config,
            m1_dir=m1_dir,
        )
        if args.estimate_only:
            print(json.dumps(estimate, ensure_ascii=False, indent=2))
            return 0
        if args.confirm_usd is None:
            print(json.dumps(estimate, ensure_ascii=False, indent=2))
            raise SystemExit("--confirm-usd is required for real M2 API run")
        _ensure_api_key()
        client = LLMClient(config=config, cache_path=args.cache)
        try:
            report = run_m2(
                document,
                args.chapters,
                design_doc=design_doc,
                config=config,
                client=client,
                out_dir=out_dir,
                m1_dir=m1_dir,
                confirm_usd=float(args.confirm_usd),
            )
        except Exception as exc:
            out_dir.mkdir(parents=True, exist_ok=True)
            report = {
                "phase": "L2A-1",
                "milestone": "M2",
                "status": "halted_transport_error",
                "prompt_source": str(design_doc),
                "model": config.model,
                "chapters_requested": args.chapters,
                "estimate": estimate,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "artifacts": {"report": str(out_dir / "m2_report.json")},
                "stop": "M2 did not complete; resolve transport/quota before retry.",
            }
            (out_dir / "m2_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.milestone == "M3":
        digest_dir = out_dir / "digest"
        estimate = estimate_m3(
            document,
            args.chapters,
            m1_dir=m1_dir,
            digest_dir=digest_dir,
        )
        if args.estimate_only:
            print(json.dumps(estimate, ensure_ascii=False, indent=2))
            return 0
        report = run_m3(
            document,
            args.chapters,
            out_dir=out_dir,
            m1_dir=m1_dir,
            digest_dir=digest_dir,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.revalidate_existing:
        report = revalidate_m1_artifacts(out_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    estimate = estimate_m1(
        document,
        args.chapters,
        design_doc=design_doc,
        config=config,
        window_target_tokens=args.window_target_tokens,
        window_max_blocks=args.window_max_blocks,
    )
    if args.estimate_only:
        print(json.dumps(estimate, ensure_ascii=False, indent=2))
        return 0
    if args.confirm_usd is None:
        print(json.dumps(estimate, ensure_ascii=False, indent=2))
        raise SystemExit("--confirm-usd is required for real M1 API run")
    _ensure_api_key()
    client = LLMClient(config=config, cache_path=args.cache)
    try:
        report = run_m1(
            document,
            args.chapters,
            design_doc=design_doc,
            config=config,
            client=client,
            out_dir=out_dir,
            confirm_usd=float(args.confirm_usd),
            window_target_tokens=args.window_target_tokens,
            window_max_blocks=args.window_max_blocks,
        )
    except Exception as exc:
        out_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "phase": "L2A-1",
            "milestone": "M1",
            "status": "halted_transport_error",
            "prompt_source": str(design_doc),
            "model": config.model,
            "chapters_requested": args.chapters,
            "estimate": estimate,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "artifacts": {"report": str(out_dir / "m1_report.json")},
            "stop": "M1 did not complete; resolve transport/quota before retry.",
        }
        (out_dir / "m1_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _ensure_api_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    for name in ["API-KEY.txt", "OPENAI-KEY.txt", "OPENAI-KEY-1.txt", "OPENAI-KEY-2.txt"]:
        key_path = REPO_ROOT / name
        if not key_path.exists():
            continue
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key
            return
    raise SystemExit("OPENAI_API_KEY is not set and no non-empty OpenAI key file was found")


def _epub_path_for_args(args: argparse.Namespace) -> Path:
    if args.epub != str(DEFAULT_EPUB):
        return Path(args.epub)
    if args.book_id == "great_gatsby":
        return DEFAULT_GATSBY_EPUB
    return DEFAULT_EPUB


def _load_document(book_id: str, epub_path: Path) -> tuple[dict, list[dict]]:
    if book_id == "great_gatsby":
        return load_great_gatsby_epub(epub_path)
    return load_wuthering_heights_epub(epub_path)


if __name__ == "__main__":
    raise SystemExit(main())
