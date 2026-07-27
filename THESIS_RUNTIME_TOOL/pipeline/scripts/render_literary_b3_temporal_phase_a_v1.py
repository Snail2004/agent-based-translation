#!/usr/bin/env python3
"""Dry-render Literary B3 temporal requests without calling an API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.literary.b3_temporal_context_v1 import load_b3_temporal_profile_v1
from pipeline.literary.b3_temporal_phase_a_v1 import (
    dry_render_b3_temporal_phase_a_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--b2-run-root", action="append", required=True, type=Path)
    parser.add_argument("--speaker-recovery-root", action="append", type=Path, default=[])
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args()
    report = dry_render_b3_temporal_phase_a_v1(
        b2_run_roots=args.b2_run_root,
        speaker_recovery_roots=args.speaker_recovery_root,
        output_root=args.output_root,
        profile=load_b3_temporal_profile_v1(args.profile),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
