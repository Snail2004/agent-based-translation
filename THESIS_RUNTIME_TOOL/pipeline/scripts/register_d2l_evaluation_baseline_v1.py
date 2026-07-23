from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.d2l_evaluation_baseline_registration_v1 import (
    register_d2l_evaluation_baseline_v1,
)
from pipeline.workflow_replay.evaluation_server_runtime_v1 import (
    validate_evaluation_server_runtime_config_v1,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Register accepted D2L Community, Google NMT and full GPT Web "
            "baseline authority for one finalized App job."
        )
    )
    parser.add_argument("--job-root", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--chapter-id",
        action="append",
        dest="chapter_ids",
        required=True,
    )
    parser.add_argument("--community-alignment-root", required=True)
    parser.add_argument(
        "--google-capture",
        action="append",
        dest="google_captures",
        required=True,
    )
    parser.add_argument("--llm-lc-marked-path", required=True)
    parser.add_argument("--llm-lc-expected-sha256", required=True)
    parser.add_argument("--llm-lc-expected-marker-count", type=int, required=True)
    parser.add_argument("--server-runtime-config", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--producer-code-commit", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime = validate_evaluation_server_runtime_config_v1(
        args.server_runtime_config
    )
    result = register_d2l_evaluation_baseline_v1(
        job_root=Path(args.job_root),
        expected_job_id=args.job_id,
        project_id=args.project_id,
        selected_chapter_ids=args.chapter_ids,
        community_alignment_root=Path(args.community_alignment_root),
        google_capture_paths=[
            Path(path) for path in args.google_captures
        ],
        llm_lc_marked_path=Path(args.llm_lc_marked_path),
        llm_lc_expected_sha256=args.llm_lc_expected_sha256,
        llm_lc_expected_marker_count=args.llm_lc_expected_marker_count,
        evaluation_profile_path=Path(runtime["llm"]["profile"]["path"]),
        created_at=args.created_at,
        producer_code_commit=args.producer_code_commit,
    )
    print(
        json.dumps(
            {
                "status": "registered",
                "workflow_runtime_path": str(
                    result.registration.workflow_runtime_path
                ),
                "baseline_template_path": str(
                    result.registration.baseline_template_path
                ),
                "registered_option_sha256": (
                    result.registered_option_sha256
                ),
                "external_arms": list(
                    result.baseline_material.artifact_paths
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
