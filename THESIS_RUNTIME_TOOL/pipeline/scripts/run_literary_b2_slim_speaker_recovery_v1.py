from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from pipeline.literary.b2_slim_speaker_recovery_v1 import (
    build_b2_slim_speaker_recovery_index_v1,
    load_b2_slim_speaker_source_v1,
    render_b2_slim_speaker_recovery_request_v1,
    request_payload_v1,
)
from pipeline.literary.checkpoint import canonical_hash


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-render B2 Slim speaker recovery")
    parser.add_argument("--b2-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output_root.resolve()
    if output.exists():
        raise SystemExit("output root already exists")
    chapter, requests = load_b2_slim_speaker_source_v1(args.b2_root)
    index = build_b2_slim_speaker_recovery_index_v1(
        chapter_artifact=chapter, interaction_requests=requests
    )
    rendered = render_b2_slim_speaker_recovery_request_v1(index)
    output.mkdir(parents=True)
    _write(output / "recovery_index.json", index)
    if rendered is not None:
        _write(output / "request.json", request_payload_v1(rendered))
    report_body: dict[str, Any] = {
        "schema_version": "literary_b2_slim_speaker_recovery_dry_report_v1",
        "status": "ready" if rendered is not None else "no_ticket_no_call",
        "chapter_id": index["chapter_id"],
        "source_b2_artifact_hash": index["source_b2_artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "ticket_count": index["counts"]["registry_gap_tickets"],
        "component_count": index["counts"]["registry_components"],
        "request_fingerprint": (
            rendered.request_fingerprint if rendered is not None else None
        ),
        "api_calls_performed": 0,
        "accepted_turn_reinspection_performed": False,
        "source_artifact_mutated": False,
        "production_publish_performed": False,
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "dry_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
