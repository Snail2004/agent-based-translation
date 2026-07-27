"""Launch a Literary chapter loop from a finalized App project."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.literary.chapter_loop_component_contract_v1 import (
    build_literary_app_run_registration_v1,
    build_literary_workflow_handoff_v1,
    validate_literary_chapter_loop_component_v1,
)
from pipeline.literary.chapter_loop_workflow_replay_adapter_v1 import (
    sync_literary_chapter_loop_replay_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.project_source_bridge_v1 import (
    prepare_literary_project_source_v1,
)
from pipeline.scripts import run_literary_chapter_loop_v1 as chapter_loop


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_chapter_loop_profile_v1.json"
)
DEFAULT_STAGE_BINDINGS = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_chapter_loop_stage_bindings_v1.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Literary pipeline from a finalized App project without "
            "mutating the canonical project source."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    _add_run_identity(init)
    init.add_argument("--runtime-bindings", type=Path, required=True)
    init.add_argument(
        "--frozen-db",
        type=Path,
        help=(
            "override the App job memory database with the frozen integrity "
            "baseline required by the selected Literary runtime"
        ),
    )
    init.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    init.add_argument("--stage-bindings", type=Path, default=DEFAULT_STAGE_BINDINGS)
    init.add_argument("--stop-after-chapter-count", type=int)
    selection = init.add_mutually_exclusive_group()
    selection.add_argument("--chapter-id", action="append")
    selection.add_argument("--chapter-range")
    selection.add_argument("--chapter-count", type=int)

    for name in ("dry-run", "status", "validate-component"):
        command = commands.add_parser(name)
        _add_run_identity(command)

    for name in ("run", "resume"):
        command = commands.add_parser(name)
        _add_run_identity(command)
        command.add_argument("--credential-file", type=Path, required=True)
        command.add_argument("--scheduler-root", type=Path, required=True)
        command.add_argument("--capability-override", action="append", default=[])
        command.add_argument("--runtime-profile-override", action="append", default=[])
        command.add_argument("--context-profile-override", action="append", default=[])
        if name == "resume":
            command.add_argument("--stop-after-chapter-count", type=int)

    sync = commands.add_parser("sync-replay")
    _add_run_identity(sync)
    sync.add_argument("--relay-root", type=Path)
    sync.add_argument("--source-package-bindings", type=Path, required=True)
    sync.add_argument("--code-commit")
    sync.add_argument("--require-terminal", action="store_true")
    sync.add_argument("--workflow-runtime-root", type=Path)
    return parser


def _add_run_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--run-id", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = _run_paths(
        jobs_root=args.jobs_root,
        job_id=args.job_id,
        run_id=args.run_id,
    )
    if args.command == "init":
        source = prepare_literary_project_source_v1(
            job_root=paths["job_root"],
            output_root=paths["source_root"],
            chapter_ids=args.chapter_id,
            chapter_range=args.chapter_range,
            chapter_count=args.chapter_count,
        )
        if source["job_id"] != args.job_id:
            raise SystemExit("selected App job ID differs from its source manifest")
        frozen_db = (
            Path(args.frozen_db).resolve()
            if args.frozen_db is not None
            else Path(source["frozen_db_path"]).resolve()
        )
        if not frozen_db.is_file():
            raise SystemExit(f"selected frozen DB does not exist: {frozen_db}")
        init_args = [
            "init",
            "--run-root",
            str(paths["component_root"]),
            "--run-id",
            args.run_id,
            "--document",
            source["document_path"],
            "--frozen-db",
            str(frozen_db),
            "--runtime-bindings",
            str(Path(args.runtime_bindings).resolve()),
            "--profile",
            str(Path(args.profile).resolve()),
            "--stage-bindings",
            str(Path(args.stage_bindings).resolve()),
            "--project-binding",
            source["project_binding_path"],
            "--all-chapters",
        ]
        if args.stop_after_chapter_count is not None:
            init_args.extend(
                [
                    "--stop-after-chapter-count",
                    str(args.stop_after_chapter_count),
                ]
            )
        result = chapter_loop.main(init_args)
        if result != 0:
            return result
        handoff = build_literary_workflow_handoff_v1(
            component_root=paths["component_root"],
            component_root_ref=paths["component_root"]
            .relative_to(paths["jobs_root"])
            .as_posix(),
        )
        _write_atomic(paths["handoff_path"], handoff)
        run_body = {
            "schema_version": "literary_app_project_run_v1",
            "project_id": source["project_id"],
            "job_id": source["job_id"],
            "source_identity_sha256": source["source_identity_sha256"],
            "workflow_run_id": args.run_id,
            "source_root_ref": paths["source_root"]
            .relative_to(paths["jobs_root"])
            .as_posix(),
            "component_root_ref": handoff["component_root_ref"],
            "workflow_handoff_ref": paths["handoff_path"]
            .relative_to(paths["jobs_root"])
            .as_posix(),
            "workflow_replay_root_ref": paths["workflow_replay_root"]
            .relative_to(paths["jobs_root"])
            .as_posix(),
            "app_run_registration_ref": paths["registration_path"]
            .relative_to(paths["jobs_root"])
            .as_posix(),
            "project_binding_hash": handoff["project_binding_hash"],
            "plan_hash": handoff["plan_hash"],
            "canonical_project_mutated": False,
            "production_publish_performed": False,
        }
        run_manifest = {**run_body, "run_manifest_hash": canonical_hash(run_body)}
        _write_atomic(paths["run_manifest_path"], run_manifest)
        _print(
            {
                "status": "initialized",
                "project_id": source["project_id"],
                "job_id": source["job_id"],
                "workflow_run_id": args.run_id,
                "chapter_count": source["chapter_count"],
                "block_count": source["block_count"],
                "component_root": str(paths["component_root"]),
                "workflow_handoff": str(paths["handoff_path"]),
                "provider_calls": 0,
                "canonical_project_mutated": False,
            }
        )
        return 0
    if args.command in {"dry-run", "status"}:
        return chapter_loop.main(
            [args.command, "--run-root", str(paths["component_root"])]
            + (["--show-plan"] if args.command == "status" else [])
        )
    if args.command in {"run", "resume"}:
        command = [
            args.command,
            "--run-root",
            str(paths["component_root"]),
            "--credential-file",
            str(Path(args.credential_file).resolve()),
            "--scheduler-root",
            str(Path(args.scheduler_root).resolve()),
        ]
        for override in args.capability_override:
            command.extend(["--capability-override", override])
        for override in args.runtime_profile_override:
            command.extend(["--runtime-profile-override", override])
        for override in args.context_profile_override:
            command.extend(["--context-profile-override", override])
        if (
            args.command == "resume"
            and args.stop_after_chapter_count is not None
        ):
            command.extend(
                [
                    "--stop-after-chapter-count",
                    str(args.stop_after_chapter_count),
                ]
            )
        return chapter_loop.main(command)
    if args.command == "validate-component":
        validation = validate_literary_chapter_loop_component_v1(
            paths["component_root"]
        )
        _print(validation["validation_receipt"])
        return 0
    if args.command == "sync-replay":
        bindings = _read_object_or_list(Path(args.source_package_bindings))
        if not isinstance(bindings, list):
            raise SystemExit("source-package bindings file must contain a list")
        relay_root = paths["workflow_replay_root"]
        if (
            args.relay_root is not None
            and Path(args.relay_root).resolve() != relay_root
        ):
            raise SystemExit(
                "relay root must match the App workflow-replay discovery path"
            )
        report = sync_literary_chapter_loop_replay_v1(
            component_root=paths["component_root"],
            handoff_path=paths["handoff_path"],
            relay_root=relay_root,
            source_package_bindings=bindings,
            code_commit=(
                args.code_commit
                or _initial_code_revision(paths["component_root"])
            ),
            require_terminal=args.require_terminal,
            workflow_runtime_root=(
                None
                if args.workflow_runtime_root is None
                else Path(args.workflow_runtime_root).resolve()
            ),
        )
        registration = build_literary_app_run_registration_v1(
            component_root=paths["component_root"],
            component_root_ref=paths["component_root"]
            .relative_to(paths["jobs_root"])
            .as_posix(),
            workflow_replay_root_ref=relay_root
            .relative_to(paths["jobs_root"])
            .as_posix(),
        )
        _write_atomic(paths["registration_path"], registration)
        _print(
            {
                **report,
                "app_run_registration": str(paths["registration_path"]),
                "app_registry_consumer_required": True,
            }
        )
        return 0
    raise SystemExit("unknown command")


def _run_paths(*, jobs_root: Path, job_id: str, run_id: str) -> dict[str, Path]:
    root = Path(jobs_root).resolve()
    safe_job = _safe_id(job_id, "job_id")
    safe_run = _safe_id(run_id, "run_id")
    container = root / "_work" / "literary" / safe_job / safe_run
    return {
        "jobs_root": root,
        "job_root": root / safe_job,
        "container": container,
        "source_root": container / "source",
        "component_root": container / "component",
        "handoff_path": container / "component" / "literary_workflow_handoff.json",
        "run_manifest_path": container / "project_run_manifest.json",
        "registration_path": container / "literary_app_run_registration.json",
        "workflow_replay_root": (
            root / "_work" / "workflow_replay" / safe_job / safe_run
        ),
    }


def _safe_id(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in value)
    ):
        raise SystemExit(f"{label} must use lowercase safe characters")
    return value


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=RUNTIME_ROOT.parent,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _initial_code_revision(component_root: Path) -> str:
    session = _read_object_or_list(
        Path(component_root).resolve() / "chapter_loop_session.json"
    )
    if not isinstance(session, Mapping):
        raise SystemExit("chapter-loop session must be an object")
    revision = session.get("code_revision")
    if not isinstance(revision, str) or not revision:
        raise SystemExit("chapter-loop session lacks its initial code revision")
    return revision


def _read_object_or_list(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot load JSON: {path}") from exc


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
