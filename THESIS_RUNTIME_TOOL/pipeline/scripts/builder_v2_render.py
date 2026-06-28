from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.prepass.builder_v2_render import (
    build_render_report,
    load_registry_entries,
    prompt_text,
    render_window,
    select_representative_windows,
)
from pipeline.prepass.db_source import load_document_from_connection
from pipeline.prepass.runner import build_d2l_prepass_windows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render Builder v2 D2L memory-pack prompts without calling an LLM."
    )
    parser.add_argument("--db", default="data/jobs/d2l_p1/memory.sqlite3")
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--chapter", required=True)
    parser.add_argument(
        "--pack-mode",
        choices=["proxy_chronological", "proxy_full_registry"],
        default="proxy_chronological",
    )
    parser.add_argument("--dry-run", action="store_true", help="Required; documents 0 API mode.")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if not args.dry_run:
        raise SystemExit("--dry-run is required for Stage B render-only")

    db_path = Path(args.db)
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        document = load_document_from_connection(
            conn,
            args.doc_id,
            [args.chapter],
            translate_only=True,
        )
        entries = load_registry_entries(conn, doc_id=args.doc_id)
    finally:
        conn.close()

    chapter = document["chapters"][0]
    windows = build_d2l_prepass_windows(chapter)
    if not windows:
        raise SystemExit(f"No Builder windows for chapter {args.chapter}")

    rendered_by_window = {
        window.window_id: render_window(entries, window, pack_mode=args.pack_mode)
        for window in windows
    }
    selected = select_representative_windows(windows, rendered_by_window)
    report = build_render_report(
        db_path=db_path,
        doc_id=args.doc_id,
        chapter_id=str(chapter["chapter_id"]),
        pack_mode=args.pack_mode,
        windows=windows,
        rendered_by_window=rendered_by_window,
        selected_windows=selected,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for label, window in selected:
        rendered = rendered_by_window[window.window_id]
        prompt_path = out_dir / f"{label}_{window.window_id}.txt"
        prompt_path.write_text(
            prompt_text(rendered["messages"]) + "\n",
            encoding="utf-8",
        )
    (out_dir / "builder_v2_b_render_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "builder_v2_b_pack_audit.json").write_text(
        json.dumps(
            {
                "selected_windows": report["selected_windows"],
                "all_windows": [
                    {
                        "window_id": window.window_id,
                        "block_ids": rendered_by_window[window.window_id]["block_ids"],
                        "audit": rendered_by_window[window.window_id]["audit"],
                        "token_estimate": rendered_by_window[window.window_id]["token_estimate"],
                    }
                    for window in windows
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "phase": report["phase"],
                "chapter_id": report["chapter_id"],
                "pack_mode": report["pack_source_mode"],
                "windows": report["windows"]["count"],
                "selected_prompts": [
                    item["prompt_file"] for item in report["selected_windows"]
                ],
                "max_prompt_tokens_est": report["windows"]["max_prompt_tokens_est"],
                "max_pack_tokens_est": report["windows"]["max_pack_tokens_est"],
                "total_prompt_tokens_est": report["windows"]["total_prompt_tokens_est"],
                "zero_api": True,
                "zero_db_write": True,
                "report": str(out_dir / "builder_v2_b_render_report.json"),
                "audit": str(out_dir / "builder_v2_b_pack_audit.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
