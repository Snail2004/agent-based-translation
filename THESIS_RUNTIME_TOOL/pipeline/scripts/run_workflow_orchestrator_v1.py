from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.workflow_replay.orchestrator_v1 import (
    ExistingTranslationComponentExecutorV1,
    StaticBaselineInputProviderV1,
    WorkflowComponentPausedV1,
    WorkflowOrchestratorError,
    WorkflowOrchestratorV1,
    load_workflow_launch_selection_v1,
    load_workflow_runtime_registration_v1,
)
from pipeline.workflow_replay.contracts_v1 import canonical_sha256


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
    translation.add_argument("--workflow-run-id")
    translation.add_argument("--component-run-id")
    translation.add_argument(
        "--evaluation-component-run-id",
        required=True,
    )
    translation.add_argument("--evaluation-root", required=True)
    translation.add_argument("--evaluation-runtime-root", required=True)
    translation.add_argument("--server-runtime-config")
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

    score = subparsers.add_parser(
        "score",
        help="Run the prepared Evaluation component under the parent relay.",
    )
    score.add_argument("--parent-root", required=True)
    score.add_argument("--job-root", required=True)
    score.add_argument("--evaluation-root", required=True)
    score.add_argument("--workflow-run-id")
    score.add_argument("--component-run-id")
    score.add_argument("--chapter-id", action="append", dest="chapter_ids")
    score.add_argument(
        "--code-root",
        default=str(Path(__file__).resolve().parents[2]),
    )
    score.add_argument("--runtime-root", required=True)
    score.add_argument("--server-runtime-config", required=True)
    score.add_argument("--resume", action="store_true")
    score_mode = score.add_mutually_exclusive_group(required=True)
    score_mode.add_argument("--dry-run", action="store_true")
    score_mode.add_argument("--live", action="store_true")

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
    if not args.resume and (
        not args.workflow_run_id
        or not args.component_run_id
        or not args.chapter_ids
    ):
        raise WorkflowOrchestratorError(
            "translation_fresh_identity",
            "Fresh Translation requires workflow, component and chapter identities.",
        )
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
        if args.live and args.server_runtime_config:
            prepared = _prepare_evaluation_runtime(
                args,
                translation_component_root=result.translation_component_root,
            )
            settings = prepared.executor_runtime.workflow_settings
            payload.update(
                {
                    "scoring_status": "ready",
                    "scoring_handoff_sha256": (
                        prepared.executor_runtime.scoring_handoff[
                            "integrity"
                        ]["handoff_sha256"]
                    ),
                    "evaluation_settings_sha256": settings[
                        "settings_sha256"
                    ],
                    "evaluation_runtime_bundle": str(
                        prepared.prepared_bundle.bundle_path
                    ),
                }
            )
        elif args.live:
            payload.update(
                {
                    "scoring_status": "pending_baseline_registration",
                    "scoring_handoff_sha256": None,
                    "evaluation_settings_sha256": None,
                    "evaluation_runtime_bundle": None,
                }
            )
    except WorkflowComponentPausedV1:
        payload = {
            "status": "translation_paused",
            "workflow_run_id": orchestrator.relay.workflow_run_id,
            "parent_status": orchestrator.relay.load_manifest()["status"],
            "translation_component_root": str(executor.component_root),
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _prepare_evaluation_runtime(
    args: argparse.Namespace,
    *,
    translation_component_root: Path,
):
    from pipeline.eval.workflow_runtime_bundle_v1 import (
        load_workflow_scoring_baseline_template_from_workflow_runtime_v1,
    )
    from pipeline.eval.workflow_runtime_factory_v1 import (
        prepare_evaluation_production_runtime_v1,
    )
    from pipeline.workflow_replay.evaluation_server_runtime_v1 import (
        build_evaluation_server_runtime_v1,
    )
    from pipeline.workflow_replay.relay_v1 import (
        validate_workflow_parent_package_v1,
    )

    if not args.server_runtime_config:
        raise WorkflowOrchestratorError(
            "evaluation_runtime_config_missing",
            "Live Translation requires the server Evaluation runtime config.",
        )
    parent_root = Path(args.parent_root).resolve()
    job_root = Path(args.job_root).resolve()
    translation_root = Path(translation_component_root).resolve()
    parent_manifest = validate_workflow_parent_package_v1(parent_root)
    launch = load_workflow_launch_selection_v1(parent_root)
    selected_chapters = launch["evaluation_selection"][
        "selected_chapter_ids"
    ]
    source_binding_sha256 = _translation_source_binding_sha256(
        translation_root
    )
    template = load_workflow_scoring_baseline_template_from_workflow_runtime_v1(
        job_root,
        expected_job_id=parent_manifest["job_id"],
        expected_source_binding_sha256=source_binding_sha256,
        selected_chapter_ids=selected_chapters,
    )
    evaluation_block_ids = _evaluation_block_ids(
        job_root,
        selected_chapter_ids=selected_chapters,
    )
    preparation = WorkflowOrchestratorV1(
        parent_root,
        translation_executor=ExistingTranslationComponentExecutorV1(
            translation_root
        ),
        baseline_provider=StaticBaselineInputProviderV1(
            template.template["external_translation_inputs"]
        ),
        selected_chapter_ids=selected_chapters,
        evaluation_block_ids=evaluation_block_ids,
    )
    handoff = preparation.prepare_scoring_handoff(translation_root)
    handoff_path = parent_root / "handoffs" / "scoring_handoff.json"
    relay_config = _relay_config(parent_root)
    component_run_id = str(args.evaluation_component_run_id)
    prepared = prepare_evaluation_production_runtime_v1(
        job_root=job_root,
        expected_job_id=parent_manifest["job_id"],
        expected_source_binding_sha256=source_binding_sha256,
        scoring_handoff_path=handoff_path,
        producer_handoff_artifacts=_producer_handoff_artifacts(
            parent_root=parent_root,
            job_root=job_root,
            handoff=handoff,
        ),
        locked_selection=launch["evaluation_selection"],
        workflow_run_id=parent_manifest["workflow_run_id"],
        component_run_id=component_run_id,
        evaluation_output_root=Path(args.evaluation_root),
        runtime_bundle_root=Path(args.evaluation_runtime_root),
        generated_at=relay_config["created_at"],
        producer_code_commit=relay_config["code_commit"],
        evaluation_logical_run_id=component_run_id,
        evaluation_attempt_run_id=f"{component_run_id}_attempt_0001",
        server_runtime=build_evaluation_server_runtime_v1(
            args.server_runtime_config
        ),
        caveats=(
            "Prepared after terminal Translation; scorer execution is a separate explicit action.",
        ),
    )
    if prepared.executor_runtime.scoring_handoff != handoff:
        raise WorkflowOrchestratorError(
            "evaluation_runtime_handoff_drift",
            "Prepared Evaluation runtime changed the parent scoring handoff.",
        )
    published_settings = preparation.relay.publish_evaluation_settings(
        prepared.executor_runtime.workflow_settings
    )
    if (
        published_settings
        != prepared.executor_runtime.workflow_settings
    ):
        raise WorkflowOrchestratorError(
            "evaluation_runtime_settings_drift",
            "Parent Evaluation settings differ from the prepared runtime.",
        )
    return prepared


def _evaluation_block_ids(
    job_root: Path,
    *,
    selected_chapter_ids: Sequence[str],
) -> list[str]:
    from pipeline.prepass.d2l_project_campaign_v2 import load_project

    project = load_project(job_root)
    selected = list(selected_chapter_ids)
    selected_set = set(selected)
    canonical_chapters = [
        str(row["chapter_id"])
        for row in project.chapter_rows
        if row["chapter_id"] in selected_set
    ]
    if canonical_chapters != selected:
        raise WorkflowOrchestratorError(
            "translation_projection_chapters",
            "Evaluation chapters differ from canonical source order.",
        )
    block_ids = [
        str(row["block_id"])
        for row in project.block_rows
        if row["chapter_id"] in selected_set
        and row["channel"] != "review_required"
    ]
    if not block_ids:
        raise WorkflowOrchestratorError(
            "translation_projection_universe",
            "Evaluation scope contains no admitted blocks.",
        )
    return block_ids


def _run_score(args: argparse.Namespace) -> int:
    from pipeline.eval.workflow_runtime_bundle_v1 import BUNDLE_FILE_NAME
    from pipeline.eval.workflow_runtime_factory_v1 import (
        build_evaluation_executor_runtime_v1,
    )
    from pipeline.workflow_replay.evaluation_server_runtime_v1 import (
        build_evaluation_server_runtime_v1,
    )
    from pipeline.workflow_replay.relay_v1 import (
        validate_workflow_parent_package_v1,
    )

    parent_root = Path(args.parent_root).resolve()
    launch = load_workflow_launch_selection_v1(parent_root)
    runtime = build_evaluation_executor_runtime_v1(
        Path(args.runtime_root).resolve() / BUNDLE_FILE_NAME,
        evaluation_output_root=Path(args.evaluation_root),
        server_runtime=build_evaluation_server_runtime_v1(
            args.server_runtime_config
        ),
    )
    parent_handoff = _read_json_file(
        parent_root / "handoffs" / "scoring_handoff.json",
        owner="parent scoring handoff",
    )
    parent_settings = _read_json_file(
        parent_root / "handoffs" / "evaluation_workflow_settings.json",
        owner="parent Evaluation settings",
    )
    if (
        runtime.scoring_handoff != parent_handoff
        or runtime.workflow_settings != parent_settings
    ):
        raise WorkflowOrchestratorError(
            "evaluation_runtime_parent_drift",
            "Prepared Evaluation runtime differs from parent authority.",
        )
    if args.dry_run:
        validate_workflow_parent_package_v1(parent_root)
        print(
            json.dumps(
                {
                    "status": "scoring_preflight_ready",
                    "workflow_run_id": parent_handoff["workflow_run_id"],
                    "component_run_id": args.component_run_id,
                    "settings_sha256": parent_settings["settings_sha256"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    orchestrator = WorkflowOrchestratorV1(
        parent_root,
        translation_executor=ExistingTranslationComponentExecutorV1(
            parent_root
        ),
        evaluation_executor=runtime.executor,
        selected_chapter_ids=launch["evaluation_selection"][
            "selected_chapter_ids"
        ],
    )
    try:
        result = orchestrator.run_prepared_scoring(
            expected_scoring_handoff=runtime.scoring_handoff,
            expected_evaluation_settings=runtime.workflow_settings,
        )
        payload = {
            "status": "scoring_succeeded",
            "workflow_run_id": result.manifest["workflow_run_id"],
            "parent_status": result.manifest["status"],
            "evaluation_component_root": str(
                result.evaluation_component_root
            ),
            "scoring_receipt_status": result.scoring_receipt["status"],
        }
    except WorkflowComponentPausedV1:
        payload = {
            "status": "scoring_paused",
            "workflow_run_id": parent_handoff["workflow_run_id"],
            "parent_status": validate_workflow_parent_package_v1(parent_root)[
                "status"
            ],
            "evaluation_component_root": str(
                Path(args.evaluation_root).resolve()
            ),
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _producer_handoff_artifacts(
    *,
    parent_root: Path,
    job_root: Path,
    handoff: Mapping[str, Any],
) -> dict[str, Path]:
    source_files = {
        "document": job_root / "source_package_snapshot" / "document.json",
        "structure_manifest": (
            job_root / "source_package_snapshot" / "structure_manifest.json"
        ),
        "asset_manifest": (
            job_root / "source_package_snapshot" / "asset_manifest.json"
        ),
        "admitted_projection": (
            job_root
            / "source_package_snapshot"
            / "admitted_projection_v1.json"
        ),
        "normalization_receipt": (
            job_root
            / "source_package_snapshot"
            / "normalization_receipt.json"
        ),
        "package_seal": (
            job_root / "lifecycle_snapshot" / "finalization.json"
        ),
    }
    result: dict[str, Path] = {}
    for row in handoff["source_package_bindings"]:
        result[row["binding"]["artifact_ref"]] = source_files[row["role"]]
    for binding in handoff["optional_bindings"].values():
        if binding is not None:
            result[binding["artifact_ref"]] = _parent_artifact_path(
                parent_root, binding["artifact_ref"]
            )
    for row in handoff["translation_inputs"]:
        if row["arm_id"] in {"s0", "s1"}:
            binding = row["translation_artifact"]
            result[binding["artifact_ref"]] = _parent_artifact_path(
                parent_root, binding["artifact_ref"]
            )
    return result


def _parent_artifact_path(parent_root: Path, artifact_ref: str) -> Path:
    if (
        not isinstance(artifact_ref, str)
        or not artifact_ref
        or "\\" in artifact_ref
    ):
        raise WorkflowOrchestratorError(
            "workflow_artifact_path",
            "Parent artifact reference is unsafe.",
        )
    path = (parent_root / Path(*artifact_ref.split("/"))).resolve()
    try:
        path.relative_to(parent_root.resolve())
    except ValueError as exc:
        raise WorkflowOrchestratorError(
            "workflow_artifact_path",
            "Parent artifact reference escapes its root.",
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise WorkflowOrchestratorError(
            "workflow_artifact_missing",
            f"Parent artifact is missing: {artifact_ref}.",
        )
    return path


def _translation_source_binding_sha256(component_root: Path) -> str:
    manifest = _read_json_file(
        component_root / "component_manifest.json",
        owner="Translation component manifest",
    )
    source_binding = manifest.get("source_binding")
    if not isinstance(source_binding, Mapping):
        raise WorkflowOrchestratorError(
            "translation_source_binding",
            "Translation component has no source binding.",
        )
    return canonical_sha256(source_binding)


def _relay_config(parent_root: Path) -> dict[str, Any]:
    row = _read_json_file(
        parent_root / "relay_config.json", owner="parent relay config"
    )
    if not isinstance(row.get("created_at"), str) or not isinstance(
        row.get("code_commit"), str
    ):
        raise WorkflowOrchestratorError(
            "workflow_parent_config",
            "Parent relay config lacks immutable creation/code identity.",
        )
    return row


def _read_json_file(path: Path, *, owner: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise WorkflowOrchestratorError(
            "workflow_artifact_missing", f"{owner} is missing."
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowOrchestratorError(
            "workflow_artifact_json", f"{owner} is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(value, dict):
        raise WorkflowOrchestratorError(
            "workflow_artifact_shape", f"{owner} must be an object."
        )
    return value


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
    if args.command == "score":
        return _run_score(args)
    return _runtime_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
