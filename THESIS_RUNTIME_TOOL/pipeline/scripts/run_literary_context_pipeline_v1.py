from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.literary.context_pipeline_profile_v1 import (
    load_context_pipeline_profile_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_LEGACY,
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
)
from pipeline.literary.literary_context_pipeline_v1 import (
    initialize_context_pipeline_run_v1,
    load_context_pipeline_state_v1,
    replay_context_pipeline_artifacts_v1,
    run_context_pipeline_live_v1,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_DOCUMENT = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_l2a0_wh_builder_scaffold"
    / "document.json"
)
DEFAULT_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_context_pipeline_openai_gpt54_v1.json"
)
DEFAULT_FROZEN_DB = (
    RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
)


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be an object")
    return value


def _print_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, indent=2))


def _selected_ids(
    document: Mapping[str, Any],
    *,
    all_chapters: bool,
    chapter_range: str | None,
) -> list[str]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise SystemExit("document has no chapters")
    ids = [
        str(row.get("chapter_id") or "")
        for row in chapters
        if isinstance(row, Mapping)
    ]
    if len(ids) != len(chapters) or not all(ids):
        raise SystemExit("document chapter ids are malformed")
    if all_chapters == bool(chapter_range):
        raise SystemExit("choose exactly one of --all-chapters or --chapter-range")
    if all_chapters:
        return ids
    assert chapter_range is not None
    parts = chapter_range.split(":")
    if len(parts) != 2 or not all(parts):
        raise SystemExit("--chapter-range must be START_ID:END_ID")
    try:
        start = ids.index(parts[0])
        end = ids.index(parts[1])
    except ValueError as exc:
        raise SystemExit("chapter range contains a foreign chapter") from exc
    if start != 0 or end < start:
        raise SystemExit("context pipeline selection must be a document prefix")
    return ids[start : end + 1]


def _plan_payload(args: argparse.Namespace) -> dict[str, Any]:
    document = _read_object(args.document, "source document")
    selected = _selected_ids(
        document,
        all_chapters=args.all_chapters,
        chapter_range=args.chapter_range,
    )
    profile = load_context_pipeline_profile_v1(args.profile)
    return {
        "schema_version": "literary_context_pipeline_plan_preview_v1",
        "profile_id": profile.profile_id,
        "profile_hash": profile.profile_hash,
        "selected_chapter_ids": selected,
        "role_bindings": dict(profile.role_bindings),
        "contract_versions": dict(profile.contract_versions),
        "recovery_stage_limits": {
            key: dict(value)
            for key, value in profile.recovery_stage_limits.items()
        },
        "limits": dict(profile.limits),
        "safety": dict(profile.safety),
        "public_stages": [
            "b1",
            "local_auditor",
            "stable_claim_auditor",
            "identity_surface_auditor",
            "b2_frame",
            "b2_interaction",
            "registry_recovery",
            "event_review",
            "context_checkpoint",
        ],
        "production_publish_enabled": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("plan", "init", "run"):
        command = commands.add_parser(name)
        command.add_argument("--run-root", type=Path)
        command.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
        command.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
        command.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
        command.add_argument(
            "--backend-mode",
            choices=(BACKEND_MODE_LEGACY, BACKEND_MODE_SHARED_V1),
            default=BACKEND_MODE_LEGACY,
        )
        selection = command.add_mutually_exclusive_group(required=True)
        selection.add_argument("--all-chapters", action="store_true")
        selection.add_argument("--chapter-range")
        if name == "run":
            command.add_argument("--credential-root", type=Path)
            command.add_argument("--usage-root", type=Path, action="append")

    resume = commands.add_parser("resume")
    resume.add_argument("--run-root", type=Path, required=True)
    resume.add_argument("--credential-root", type=Path)
    resume.add_argument(
        "--backend-mode",
        choices=(BACKEND_MODE_LEGACY, BACKEND_MODE_SHARED_V1),
        default=BACKEND_MODE_LEGACY,
    )
    resume.add_argument("--usage-root", type=Path, action="append")

    status = commands.add_parser("status")
    status.add_argument("--run-root", type=Path, required=True)

    replay = commands.add_parser("replay")
    replay.add_argument("--output-root", type=Path, required=True)
    replay.add_argument("--b1-root", type=Path, required=True)
    replay.add_argument("--chapter-id", action="append", required=True)
    replay.add_argument("--b2-root", type=Path, action="append", required=True)
    replay.add_argument(
        "--recovery-root", type=Path, action="append", required=True
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    current_head = _git_head()
    if args.command == "plan":
        preview = _plan_payload(args)
        preview["backend_mode"] = args.backend_mode
        preview["shared_runtime_injection_required"] = (
            args.backend_mode == BACKEND_MODE_SHARED_V1
        )
        _print_json(preview)
        return 0
    if args.command in {"init", "run"}:
        if args.run_root is None:
            raise SystemExit("--run-root is required")
        if not (args.run_root / "run_plan.json").is_file():
            document = _read_object(args.document, "source document")
            selected = _selected_ids(
                document,
                all_chapters=args.all_chapters,
                chapter_range=args.chapter_range,
            )
            initialize_context_pipeline_run_v1(
                run_root=args.run_root,
                document_path=args.document,
                profile_path=args.profile,
                frozen_db=args.frozen_db,
                ordered_chapter_ids=selected,
                current_git_head=current_head,
                backend_mode=args.backend_mode,
                shared_runtime=shared_runtime,
            )
        if args.command == "init":
            _print_json(load_context_pipeline_state_v1(args.run_root))
            return 0
        _print_json(
            run_context_pipeline_live_v1(
                run_root=args.run_root,
                credential_root=args.credential_root,
                current_git_head=current_head,
                usage_roots=args.usage_root,
                backend_mode=args.backend_mode,
                shared_runtime=shared_runtime,
            )
        )
        return 0
    if args.command == "resume":
        _print_json(
            run_context_pipeline_live_v1(
                run_root=args.run_root,
                credential_root=args.credential_root,
                current_git_head=current_head,
                usage_roots=args.usage_root,
                allow_resume=True,
                backend_mode=args.backend_mode,
                shared_runtime=shared_runtime,
            )
        )
        return 0
    if args.command == "status":
        _print_json(load_context_pipeline_state_v1(args.run_root))
        return 0
    if args.command == "replay":
        if not (
            len(args.chapter_id)
            == len(args.b2_root)
            == len(args.recovery_root)
        ):
            raise SystemExit(
                "--chapter-id, --b2-root, and --recovery-root counts differ"
            )
        rows = [
            {
                "chapter_id": chapter_id,
                "b2_root": str(b2_root),
                "recovery_root": str(recovery_root),
            }
            for chapter_id, b2_root, recovery_root in zip(
                args.chapter_id, args.b2_root, args.recovery_root
            )
        ]
        _print_json(
            replay_context_pipeline_artifacts_v1(
                output_root=args.output_root,
                b1_root=args.b1_root,
                chapter_artifacts=rows,
                current_git_head=current_head,
            )
        )
        return 0
    raise SystemExit("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
