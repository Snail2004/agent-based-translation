"""Render and measure the bounded B1-Scan memory projection with zero API calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.literary.b1_scan_v1 import (
    render_b1_scan_request_v1,
    shared_b1_scan_request_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.openai_b1_scan_capability_probe_v1 import DESIGN_DOC, ROLE_ID
from pipeline.literary.request_token_preflight_v1 import (
    measure_literary_request_token_preflight_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.scripts.run_literary_b1_scan_v1 import (
    _load_prior_cards_context,
    _load_summary_context,
)
from pipeline.scripts.run_literary_builder_pilot import DEFAULT_EPUB, _load_document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--epub", type=Path, default=DEFAULT_EPUB)
    prior = parser.add_mutually_exclusive_group(required=True)
    prior.add_argument("--prior-registry-root", type=Path)
    prior.add_argument("--prior-projection-root", type=Path)
    parser.add_argument("--previous-summary-root", required=True, type=Path)
    parser.add_argument("--runtime-profile", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.out_dir.exists():
        raise SystemExit(f"out dir already exists: {args.out_dir}")
    runtime = load_literary_shared_runtime_profile_v2(
        args.runtime_profile,
        expected_role_ids={ROLE_ID},
    )
    preset = runtime.role_presets[ROLE_ID]
    memory_token_budget = preset.generation.get("memory_token_budget")
    if memory_token_budget is None:
        raise SystemExit("runtime profile has no B1-Scan memory_token_budget")

    document, _mapping = _load_document("wuthering_heights", args.epub)
    chapter_by_id = {row["chapter_id"]: row for row in document["chapters"]}
    try:
        chapter = chapter_by_id[args.chapter]
    except KeyError as exc:
        raise SystemExit(f"chapter is absent: {args.chapter}") from exc
    order_by_id = {
        row["chapter_id"]: index
        for index, row in enumerate(document["chapters"], start=1)
    }
    chapter_order = order_by_id[args.chapter]
    prior_cards, lineage = _load_prior_cards_context(
        prior_cards_path=None,
        prior_registry_root=args.prior_registry_root,
        prior_projection_root=args.prior_projection_root,
    )
    previous_summary, global_summary, summary_lineage = _load_summary_context(
        previous_summary_root=args.previous_summary_root,
        current_chapter_order=chapter_order,
    )
    rendered = render_b1_scan_request_v1(
        chapter=chapter,
        design_doc=DESIGN_DOC,
        prior_cards=prior_cards,
        previous_chapter_summary=previous_summary,
        global_summary=global_summary,
        memory_token_budget=int(memory_token_budget),
        memory_dormancy_chapters=int(
            preset.generation.get("memory_dormancy_chapters", 3)
        ),
        chapter_order_by_id=order_by_id,
    )
    request = shared_b1_scan_request_v1(rendered)
    memory = rendered.sections.get("memory_budget_report")
    if not isinstance(memory, dict):
        raise SystemExit("bounded memory report was not rendered")
    token_preflight = measure_literary_request_token_preflight_v1(
        request,
        prompt_token_cap=int(preset.generation["max_input_tokens"]),
        output_token_cap=int(preset.generation["max_output_tokens"]),
    )
    blocks = chapter.get("blocks") or []
    text_chars = sum(
        len(str(row.get("clean_text", row.get("text", "")))) for row in blocks
    )
    report_body = {
        "schema_version": "literary_b1_scan_memory_replay_report_v1",
        "chapter_id": args.chapter,
        "chapter_order": chapter_order,
        "chapter_block_count": len(blocks),
        "chapter_text_chars": text_chars,
        "prior_card_count": len(prior_cards),
        "memory_budget_report": memory,
        "request_fingerprint": rendered.request_fingerprint,
        "runtime_profile_sha256": runtime.profile_sha256,
        "lineage": {**lineage, **summary_lineage},
        "token_preflight": token_preflight.to_payload(),
        "provider_calls": 0,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}

    args.out_dir.mkdir(parents=True, exist_ok=False)
    _write(
        args.out_dir / "request.json",
        {
            **rendered.to_dict(),
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": request["response_schema"],
        },
    )
    _write(args.out_dir / "memory_budget_report.json", memory)
    _write(args.out_dir / "token_preflight.json", token_preflight.to_payload())
    _write(args.out_dir / "replay_report.json", report)
    print(
        f"{args.chapter}: memory={memory['memory_tokens_used']}/"
        f"{memory['memory_token_budget']} "
        f"admitted={memory['admitted']} omitted={memory['omitted_counts']} "
        f"prompt={token_preflight.prompt_token_reserve}/"
        f"{token_preflight.prompt_token_cap} "
        f"blocks={len(blocks)} chars={text_chars} provider_calls=0"
    )
    return 0


def _write(path: Path, value: object) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
