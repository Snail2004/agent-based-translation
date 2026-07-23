from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from pipeline.workflow_replay.orchestrator_v1 import (
    WorkflowComponentPausedV1,
    WorkflowOrchestratorError,
    WorkflowOrchestratorV1,
    load_workflow_runtime_registration_v1,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the neutral translation/evaluation/publication workflow."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    translation = subparsers.add_parser(
        "translate",
        help="Run only the D2L Translation component under the parent relay.",
    )
    translation.add_argument("--parent-root", required=True)
    translation.add_argument("--job-root", required=True)
    translation.add_argument("--campaign-root", required=True)
    translation.add_argument("--workflow-run-id", required=True)
    translation.add_argument("--component-run-id", required=True)
    translation.add_argument("--chapter-id", action="append", dest="chapter_ids")
    translation.add_argument("--hard-total-token-cap", type=int)
    translation.add_argument("--reserved-cost-cap-usd")
    translation.add_argument(
        "--code-root",
        default=str(Path(__file__).resolve().parents[2]),
    )
    translation.add_argument("--runtime-root")
    translation.add_argument("--credential-file", action="append", default=[])
    translation.add_argument("--resume", action="store_true")
    mode = translation.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")

    runtime = subparsers.add_parser(
        "runtime-status",
        help="Validate one server-owned workflow runtime registration.",
    )
    runtime.add_argument("--job-root", required=True)
    runtime.add_argument("--job-id", required=True)
    runtime.add_argument("--source-binding-sha256", required=True)
    runtime.add_argument("--chapter-id", action="append", dest="chapter_ids")
    return parser


class _D2LSubprocessExecutorV1:
    def __init__(
        self,
        *,
        job_root: Path,
        campaign_root: Path,
        workflow_run_id: str,
        component_run_id: str,
        chapter_ids: Sequence[str],
        hard_total_token_cap: int | None,
        reserved_cost_cap_usd: str | None,
        code_root: Path,
        runtime_root: Path | None,
        credential_files: Sequence[str],
        live: bool,
        resume: bool,
    ) -> None:
        self.job_root = job_root.resolve()
        self.campaign_root = campaign_root.resolve()
        self.component_root = self.campaign_root / "component"
        self.workflow_run_id = workflow_run_id
        self.component_run_id = component_run_id
        self.chapter_ids = tuple(chapter_ids)
        self.hard_total_token_cap = hard_total_token_cap
        self.reserved_cost_cap_usd = reserved_cost_cap_usd
        self.code_root = code_root.resolve()
        self.runtime_root = runtime_root.resolve() if runtime_root else None
        self.credential_files = tuple(credential_files)
        self.live = live
        self.resume = resume

    def execute(self, observer) -> Path:
        argv = self._argv()
        process = subprocess.Popen(argv, cwd=self.code_root)
        last_signature = None
        while process.poll() is None:
            signature = _component_signature(self.component_root)
            if signature is not None and signature != last_signature:
                try:
                    observer(self.component_root, False)
                    last_signature = signature
                except Exception:
                    # Component writers publish several immutable files before
                    # advancing the current manifest. A transient incomplete
                    # prefix is retried; terminal validation below remains strict.
                    pass
            time.sleep(0.1)
        return_code = process.wait()
        manifest = _read_component_manifest(self.component_root)
        status = manifest.get("status")
        if status == "paused":
            observer(self.component_root, False)
            raise WorkflowComponentPausedV1("translation")
        if return_code != 0 or status != "succeeded":
            raise WorkflowOrchestratorError(
                "translation_process_failed",
                f"D2L Translation exited {return_code} with component status {status!r}.",
            )
        return self.component_root

    def _argv(self) -> list[str]:
        argv = [
            sys.executable,
            "-m",
            "pipeline.scripts.run_d2l_project_campaign",
            "app-run",
            "--job-root",
            str(self.job_root),
            "--campaign-root",
            str(self.campaign_root),
            "--code-root",
            str(self.code_root),
        ]
        if self.resume:
            argv.append("--resume")
        else:
            if not self.chapter_ids:
                raise WorkflowOrchestratorError(
                    "translation_chapters",
                    "Fresh Translation requires at least one chapter.",
                )
            argv.extend(["--workflow-run-id", self.workflow_run_id])
            argv.extend(["--component-run-id", self.component_run_id])
            for chapter_id in self.chapter_ids:
                argv.extend(["--chapter-id", chapter_id])
        if self.hard_total_token_cap is not None:
            argv.extend(
                ["--hard-total-token-cap", str(self.hard_total_token_cap)]
            )
        if self.reserved_cost_cap_usd is not None:
            argv.extend(
                ["--reserved-cost-cap-usd", self.reserved_cost_cap_usd]
            )
        argv.append("--live" if self.live else "--dry-run")
        if self.runtime_root is not None:
            argv.extend(["--runtime-root", str(self.runtime_root)])
        if self.live:
            for value in self.credential_files:
                argv.extend(["--credential-file", value])
        return argv


def _run_translation(args: argparse.Namespace) -> int:
    parent_root = Path(args.parent_root).resolve()
    executor = _D2LSubprocessExecutorV1(
        job_root=Path(args.job_root),
        campaign_root=Path(args.campaign_root),
        workflow_run_id=args.workflow_run_id,
        component_run_id=args.component_run_id,
        chapter_ids=args.chapter_ids or (),
        hard_total_token_cap=args.hard_total_token_cap,
        reserved_cost_cap_usd=args.reserved_cost_cap_usd,
        code_root=Path(args.code_root),
        runtime_root=Path(args.runtime_root) if args.runtime_root else None,
        credential_files=args.credential_file,
        live=bool(args.live),
        resume=bool(args.resume),
    )
    orchestrator = WorkflowOrchestratorV1(
        parent_root,
        translation_executor=executor,
        selected_chapter_ids=args.chapter_ids or _selected_chapters(parent_root),
    )
    try:
        result = orchestrator.run_translation()
        payload: dict[str, Any] = {
            "status": "translation_succeeded",
            "workflow_run_id": result.manifest["workflow_run_id"],
            "parent_status": result.manifest["status"],
            "translation_component_root": str(
                result.translation_component_root
            ),
        }
    except WorkflowComponentPausedV1:
        payload = {
            "status": "translation_paused",
            "workflow_run_id": orchestrator.relay.workflow_run_id,
            "parent_status": orchestrator.relay.load_manifest()["status"],
            "translation_component_root": str(executor.component_root),
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _runtime_status(args: argparse.Namespace) -> int:
    registration = load_workflow_runtime_registration_v1(
        args.job_root,
        expected_job_id=args.job_id,
        expected_source_binding_sha256=args.source_binding_sha256,
        selected_chapter_ids=args.chapter_ids,
    )
    print(
        json.dumps(
            {
                "status": registration["status"],
                "blockers": registration["blockers"],
                "registration_sha256": registration["integrity"][
                    "registration_sha256"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if registration["status"] == "ready" else 3


def _selected_chapters(parent_root: Path) -> list[str]:
    config_path = parent_root / "relay_config.json"
    if not config_path.is_file() or config_path.is_symlink():
        raise WorkflowOrchestratorError(
            "workflow_parent_config",
            "Parent relay config is missing.",
        )
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowOrchestratorError(
            "workflow_parent_config",
            "Parent relay config is not valid UTF-8 JSON.",
        ) from exc
    stages = value.get("stages") if isinstance(value, dict) else None
    if not isinstance(stages, list):
        raise WorkflowOrchestratorError(
            "workflow_parent_config",
            "Parent relay config has no stage plan.",
        )
    chapters = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        local = stage.get("local_stage_id")
        if (
            stage.get("component_id") == "evaluation"
            and isinstance(local, str)
            and local.startswith("chapter_")
        ):
            chapters.append(local.removeprefix("chapter_"))
    if not chapters:
        raise WorkflowOrchestratorError(
            "workflow_parent_chapters",
            "Parent relay does not bind selected chapters.",
        )
    return chapters


def _component_signature(root: Path) -> tuple[tuple[str, int, int], ...] | None:
    paths = (
        root / "component_manifest.json",
        root / "events.jsonl",
        root / "artifact_index.json",
    )
    if not all(path.is_file() and not path.is_symlink() for path in paths):
        return None
    return tuple(
        (path.name, path.stat().st_size, path.stat().st_mtime_ns)
        for path in paths
    )


def _read_component_manifest(root: Path) -> dict[str, Any]:
    path = root / "component_manifest.json"
    if not path.is_file() or path.is_symlink():
        raise WorkflowOrchestratorError(
            "translation_manifest_missing",
            "D2L component manifest is missing after child exit.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowOrchestratorError(
            "translation_manifest_invalid",
            "D2L component manifest is not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(value, dict):
        raise WorkflowOrchestratorError(
            "translation_manifest_invalid",
            "D2L component manifest must be an object.",
        )
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "translate":
        return _run_translation(args)
    return _runtime_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
