"""Offline dry-run CLI: hearing queue -> prepared cross-chapter Auditor requests.

Task A scope only: verify + partition + render + write immutable artifacts.
This script performs ZERO provider calls and grants no authority.  The rendered
``model_contract`` is an explicit non-executable placeholder unless overridden;
a later live consumer must re-render under its own sealed profile.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = _REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.b1_cross_chapter_audit_bridge_v1 import (
    B1CrossChapterAuditBridgeError,
    build_cross_chapter_audit_dry_run_v1,
)


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _collect_source_blocks(chapter_paths: list[Path]) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for path in chapter_paths:
        chapter = _load_json(path)
        raw = chapter.get("blocks")
        if not isinstance(raw, list) or not raw:
            raise B1CrossChapterAuditBridgeError(
                f"chapter file has no source blocks: {path}"
            )
        for row in raw:
            block_id = row.get("block_id")
            text = row.get("text")
            if not isinstance(block_id, str) or not isinstance(text, str):
                raise B1CrossChapterAuditBridgeError(
                    f"chapter file has a malformed block: {path}"
                )
            if block_id in blocks and blocks[block_id] != text:
                raise B1CrossChapterAuditBridgeError(
                    f"conflicting text supplied twice for block: {block_id}"
                )
            blocks[block_id] = text
    return blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument(
        "--chapter",
        action="append",
        required=True,
        type=Path,
        help="chapter source JSON with blocks[]; repeat for prior chapters",
    )
    parser.add_argument("--design-doc", type=Path, default=DESIGN_DOC)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--model-id", default="dry_run_unbound")
    parser.add_argument("--reasoning-effort", default="none")
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument("--max-output-tokens", type=int, default=2500)
    parser.add_argument(
        "--expand-prior-evidence",
        action="store_true",
        help="include bounded prior-side context; default is the narrow arm",
    )
    args = parser.parse_args()

    if args.out_dir.exists():
        raise SystemExit(f"out dir already exists (immutable artifacts): {args.out_dir}")

    queue = _load_json(args.queue)
    registry = _load_json(args.registry)
    verify_b1_chapter_registry_v1(registry)
    source_blocks = _collect_source_blocks(list(args.chapter))
    model_contract = {
        "model_id": args.model_id,
        "reasoning_effort": args.reasoning_effort,
        "temperature": args.temperature,
        "seed": args.seed,
        "max_output_tokens": args.max_output_tokens,
    }
    report = build_cross_chapter_audit_dry_run_v1(
        queue=queue,
        source_blocks=source_blocks,
        design_doc=args.design_doc,
        model_contract=model_contract,
        expected_registry_hash=registry["registry_hash"],
        expand_prior_evidence=args.expand_prior_evidence,
    )

    requests_dir = args.out_dir / "prepared_requests"
    requests_dir.mkdir(parents=True, exist_ok=False)
    for request in report["prepared_requests"]:
        target = requests_dir / f"{request['component_id']}.json"
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(request, handle, ensure_ascii=False, indent=1, sort_keys=True)
    with open(args.out_dir / "dry_run_report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1, sort_keys=True)

    coverage = report["coverage"]
    print(
        "cross-chapter audit dry run: "
        f"components={coverage['component_count']} "
        f"prepared={coverage['prepared_count']} "
        f"waiting={coverage['waiting_count']} "
        f"unconsumed_ready={coverage['unconsumed_ready_count']} "
        f"provider_calls={report['provider_calls']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
