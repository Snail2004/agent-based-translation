from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import quote

from pipeline.eval.evaluation_workflow_settings_v1 import (
    EVALUATION_CHAPTER_IDS_V1,
    EVALUATION_SCORER_IDS_V1,
)
from pipeline.workflow_replay.contracts_v1 import (
    WorkflowReplayContractError,
    canonical_sha256,
    physical_sha256,
    validate_workflow_artifact_index_v1,
    validate_workflow_event_v1,
)
from pipeline.workflow_replay.relay_v1 import validate_workflow_parent_package_v1
from services.thesis_runs import validate_job_id, validate_run_id


WORKFLOW_REPLAY_READ_SCHEMA = "workflow_replay_read_v1"
WORKFLOW_LIVE_CONSOLE_SCHEMA = "workflow_live_console_v1"
WORKFLOW_ARTIFACT_READ_SCHEMA = "workflow_artifact_read_v1"
WORKFLOW_SETUP_SCHEMA_ID = "WorkflowSetupV1"
WORKFLOW_PREFLIGHT_SCHEMA_ID = "WorkflowPreflightV1"
WORKFLOW_SELECTION_SCHEMA_ID = "WorkflowSetupSelectionV1"
WORKFLOW_LAUNCH_SCHEMA_ID = "WorkflowLaunchConfirmationV1"
WORKFLOW_APP_SCHEMA_VERSION = "1.0.0"
WORKFLOW_SELECTION_SCHEMA_VERSION = "1.1.0"
EVALUATION_SETTINGS_SCHEMA_ID = "EvaluationWorkflowSettingsV1"
EVALUATION_SETTINGS_SCHEMA_VERSION = "1.1.0"
# This launch contract starts the Translation phase only. Scoring is a separate
# parent-workflow action and remains project-specific and fail-closed.
LIVE_START_ALLOWED = True
MAX_WAIT_MS = 20_000
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_PARENT_JSON_BYTES = 64 * 1024 * 1024
MAX_EVENT_STREAM_BYTES = 128 * 1024 * 1024
MAX_EVENT_COUNT = 100_000
PREFLIGHT_TTL_SECONDS = 30 * 60
MAX_ACTIVE_PREFLIGHTS = 64
LIVE_CONSOLE_EVENT_LIMIT = 400
LIVE_CONSOLE_PAYLOAD_LIMIT = 8 * 1024
LIVE_CONSOLE_TAIL_BYTES = 4 * 1024 * 1024

SHARED_SETTINGS_ID = "shared_llm_transport_catalog_v1"
D2L_SETTINGS_ID = "d2l_workflow_settings_v1"
EVALUATION_SETTINGS_ID = "evaluation_workflow_settings_v1"
EVALUATION_ARM_ORDER = ("s0", "s1", "community", "google_nmt", "llm_lc")
EVALUATION_CHAPTER_ORDER = tuple(EVALUATION_CHAPTER_IDS_V1)
EVALUATION_SCORER_ORDER = tuple(EVALUATION_SCORER_IDS_V1)

_CREDENTIAL_BINDINGS = (
    (
        "credential.shopaikey_gemini_proxy_v1",
        "THESIS_D2L_SHOPAPI_CREDENTIAL_FILE",
    ),
    ("credential.modelapi_shared_v1", "THESIS_D2L_MODELAPI_CREDENTIAL_FILE"),
)


@dataclass(frozen=True)
class _WorkflowPreflightToken:
    token: str
    expires_at: float
    public: dict[str, Any]
    launch: dict[str, Any]


_preflight_tokens: dict[str, _WorkflowPreflightToken] = {}
_preflight_lock = threading.Lock()
_live_console_cache: dict[str, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
_live_console_cache_lock = threading.Lock()


class WorkflowReplayError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _scoring_runtime_readiness(
    *,
    job_root: str | Path,
    job_id: str,
    source_binding_sha256: str,
    selected_chapter_ids: list[str] | None = None,
) -> dict[str, Any]:
    from services.thesis_runs import (
        RunControlError,
        _evaluation_runtime_config_file,
    )
    from pipeline.workflow_replay.orchestrator_v1 import (
        WorkflowOrchestratorError,
        load_workflow_runtime_registration_v1,
    )
    from pipeline.workflow_replay.evaluation_server_runtime_v1 import (
        EvaluationServerRuntimeError,
        validate_evaluation_server_runtime_config_v1,
    )

    executor_blockers: list[str] = []
    try:
        runtime_config = _evaluation_runtime_config_file(required=True)
        assert runtime_config is not None
        validate_evaluation_server_runtime_config_v1(runtime_config)
    except RunControlError as exc:
        executor_blockers.append(exc.code)
    except EvaluationServerRuntimeError as exc:
        executor_blockers.append(exc.code)
    try:
        registration = load_workflow_runtime_registration_v1(
            job_root,
            expected_job_id=job_id,
            expected_source_binding_sha256=source_binding_sha256,
            selected_chapter_ids=selected_chapter_ids,
        )
    except (OSError, WorkflowOrchestratorError) as exc:
        blockers = [
            getattr(exc, "code", "workflow_runtime_registration_invalid")
        ]
        blockers.extend(executor_blockers)
        return {
            "allowed": False,
            "blockers": blockers,
            "runtime": {
                "status": "blocked",
                "registration_sha256": None,
            },
        }
    blockers = list(registration["blockers"])
    blockers.extend(executor_blockers)
    return {
        "allowed": registration["status"] == "ready" and not blockers,
        "blockers": blockers,
        "runtime": {
            "status": registration["status"],
            "registration_sha256": registration["integrity"][
                "registration_sha256"
            ],
            "supported_chapter_ids": list(
                registration["supported_chapter_ids"]
            ),
            "baseline_status": registration["baseline_bundle"]["status"],
            "evaluation_executor_id": registration[
                "evaluation_executor_id"
            ],
            "publication_executor_id": registration[
                "publication_executor_id"
            ],
        },
    }


def get_workflow_setup(
    doc_id: str,
    *,
    jobs_root: str | Path,
) -> dict[str, Any]:
    status, loaded_project = _load_setup_project(doc_id, jobs_root=jobs_root)
    chapter_counts: dict[str, int] = {}
    for block in loaded_project.block_rows:
        chapter_id = str(block["chapter_id"])
        chapter_counts[chapter_id] = chapter_counts.get(chapter_id, 0) + 1
    chapters = [
        {
            "chapter_id": row["chapter_id"],
            "title": row.get("title") or row["chapter_id"],
            "block_count": chapter_counts.get(row["chapter_id"], 0),
            "selectable": True,
        }
        for row in loaded_project.chapter_rows
        if row["chapter_id"]
        in set(loaded_project.manifest["translatable_chapter_ids"])
    ]
    source_package = {
        "project_id": doc_id,
        "doc_id": doc_id,
        "source_package_tree_sha256": loaded_project.source_snapshot[
            "package_tree_sha256"
        ],
        "source_binding_sha256": canonical_sha256(
            loaded_project.source_binding
        ),
        "source_package_bindings": _parent_source_bindings(
            loaded_project.source_binding
        ),
    }
    runtime = {
        "job_id": status["job_id"],
        "managed": bool(status.get("managed")),
        "prepared": bool(status.get("prepared")),
        "lifecycle": status.get("lifecycle"),
        "source_identity_sha256": status.get("source_identity_sha256"),
    }
    shared_option = _shared_settings_option()
    d2l_option = _d2l_settings_option()
    available_chapter_ids = [row["chapter_id"] for row in chapters]
    evaluation_option = _evaluation_settings_option(
        available_chapter_ids=available_chapter_ids,
        job_root=(Path(jobs_root).resolve() / status["job_id"]),
        job_id=status["job_id"],
        source_binding_sha256=source_package["source_binding_sha256"],
    )
    default_chapter_ids = set(
        evaluation_option["default_selection"]["selected_chapter_ids"]
    )
    for chapter in chapters:
        chapter["selected_by_default"] = (
            chapter["chapter_id"] in default_chapter_ids
        )
    scoring_readiness = _scoring_runtime_readiness(
        job_root=(Path(jobs_root).resolve() / status["job_id"]),
        job_id=status["job_id"],
        source_binding_sha256=source_package["source_binding_sha256"],
    )
    payload = {
        "schema_id": WORKFLOW_SETUP_SCHEMA_ID,
        "schema_version": WORKFLOW_APP_SCHEMA_VERSION,
        "project_id": doc_id,
        "source_package": source_package,
        "runtime": runtime,
        "chapters": chapters,
        "execution_modes": [
            {"id": "dry_run", "label": "Dry run", "enabled": True},
            {
                "id": "live",
                "label": "Live",
                "enabled": LIVE_START_ALLOWED,
                "reason": (
                    None
                    if LIVE_START_ALLOWED
                    else "translation_live_start_disabled"
                ),
            },
        ],
        "live_start_allowed": LIVE_START_ALLOWED,
        "launch_phase": "translation",
        "scoring_start_allowed": scoring_readiness["allowed"],
        "scoring_blocking_reasons": scoring_readiness["blockers"],
        "scoring_runtime": scoring_readiness["runtime"],
        "dry_run_allowed": True,
        "shared_options": [shared_option],
        "d2l_settings_options": [d2l_option],
        "evaluation_settings_options": [evaluation_option],
        "defaults": {
            "shared_option_id": SHARED_SETTINGS_ID,
            "d2l_settings_option_id": D2L_SETTINGS_ID,
            "evaluation": dict(evaluation_option["default_selection"]),
            "hard_total_token_cap": 6_000_000,
            "reserved_cost_cap_usd": None,
        },
        "constraints": {
            "selectable_fields": [
                "execution_mode",
                "chapter_ids",
                "evaluation",
                "hard_total_token_cap",
                "reserved_cost_cap_usd",
            ],
            "server_fixed_fields": [
                "source_id",
                "source_revision",
                "requested_model_id",
                "output_mode",
                "stages",
                "translation_arms",
                "evaluation_registered_universe",
                "retry_policy",
                "fallback_policy",
                "cache_policy",
                "validators",
                "thresholds",
            ],
        },
    }
    payload["setup_sha256"] = canonical_sha256(payload)
    return payload


def create_workflow_preflight(
    doc_id: str,
    request_body: Any,
    *,
    planned_run_id: str,
    jobs_root: str | Path,
    tool_root: str | Path,
    python_exe: str | None = None,
) -> dict[str, Any]:
    body = _validate_preflight_request(request_body)
    setup = get_workflow_setup(doc_id, jobs_root=jobs_root)
    if body["shared_option_id"] != SHARED_SETTINGS_ID:
        raise WorkflowReplayError(
            "workflow_shared_settings_invalid",
            "shared_settings_id is not advertised by the server.",
        )
    if body["d2l_settings_option_id"] != D2L_SETTINGS_ID:
        raise WorkflowReplayError(
            "workflow_d2l_settings_invalid",
            "d2l_settings_id is not advertised by the server.",
        )
    if body["evaluation"]["settings_option_id"] != EVALUATION_SETTINGS_ID:
        raise WorkflowReplayError(
            "workflow_evaluation_settings_invalid",
            "evaluation_settings_id is not advertised by the server.",
        )
    available_chapter_order = [
        row["chapter_id"] for row in setup["chapters"] if row["selectable"]
    ]
    if not _is_ordered_subset(
        body["chapter_ids"],
        allowed=available_chapter_order,
        minimum=1,
    ):
        raise WorkflowReplayError(
            "workflow_chapters_invalid",
            "chapter_ids must be a canonical non-empty subset of the advertised chapters.",
        )
    evaluation_option = setup["evaluation_settings_options"][0]
    evaluation_selection = _normalize_evaluation_selection(
        body["evaluation"],
        parent_chapter_ids=body["chapter_ids"],
        option=evaluation_option,
    )
    run_id = validate_run_id(planned_run_id, required=True)
    if run_id is None:
        raise WorkflowReplayError(
            "workflow_run_identity_invalid",
            "Server could not allocate a planned run identity.",
            500,
        )

    from services.thesis_runs import (
        _d2l_preview_source,
        d2l_component_ids,
    )

    identities = d2l_component_ids(run_id)
    try:
        preview = _d2l_preview_source(
            job_root=(
                Path(jobs_root).resolve() / setup["runtime"]["job_id"]
            ),
            chapters=list(body["chapter_ids"]),
            workflow_run_id=identities["workflow_run_id"],
            component_run_id=identities["component_run_id"],
            hard_total_token_cap=body["hard_total_token_cap"],
            reserved_cost_cap_usd=body["reserved_cost_cap_usd"],
            tool_root=Path(tool_root).resolve(),
        )
    except Exception as exc:
        if isinstance(exc, WorkflowReplayError):
            raise
        code = getattr(exc, "code", "workflow_preflight_failed")
        status = int(getattr(exc, "status", 409))
        message = getattr(exc, "message", str(exc))
        raise WorkflowReplayError(code, message, status) from exc

    credentials = _credential_status()
    missing_credentials = [
        row["credential_ref"]
        for row in credentials
        if row["status"] != "available"
    ]
    mode = body["execution_mode"]
    scoring_readiness = _scoring_runtime_readiness(
        job_root=(Path(jobs_root).resolve() / setup["runtime"]["job_id"]),
        job_id=setup["runtime"]["job_id"],
        source_binding_sha256=setup["source_package"][
            "source_binding_sha256"
        ],
        selected_chapter_ids=evaluation_selection["selected_chapter_ids"],
    )
    blocking_reasons = []
    if mode == "live" and not LIVE_START_ALLOWED:
        blocking_reasons.append("workflow_live_start_disabled")
    if mode == "live" and missing_credentials:
        blocking_reasons.append("workflow_credentials_unavailable")
    blocking_reasons = list(dict.fromkeys(blocking_reasons))
    api_confirm_token = None
    if mode == "live" and not blocking_reasons:
        api_confirm_token = _issue_live_api_token(
            job_id=setup["runtime"]["job_id"],
            planned_run_id=run_id,
            chapter_ids=list(body["chapter_ids"]),
            hard_total_token_cap=body["hard_total_token_cap"],
            reserved_cost_cap_usd=body["reserved_cost_cap_usd"],
            preview=preview,
            jobs_root=jobs_root,
            tool_root=tool_root,
            python_exe=python_exe,
            prepare_scoring=scoring_readiness["allowed"],
        )
    issued_at = _utc_now()
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=PREFLIGHT_TTL_SECONDS)
    ).isoformat().replace("+00:00", "Z")
    preflight_id = f"preflight_{run_id}"
    launch = {
        "script": "run_workflow_orchestrator_v1",
        "phase": "translation",
        "mode": mode,
        "allow_api": mode == "live",
        "doc_id": doc_id,
        "job_id": setup["runtime"]["job_id"],
        "planned_run_id": run_id,
        "workflow_run_id": identities["workflow_run_id"],
        "component_run_id": identities["component_run_id"],
        "chapter_ids": list(body["chapter_ids"]),
        "profile_id": "technical_d2l_v1",
        "hard_total_token_cap": body["hard_total_token_cap"],
        "reserved_cost_cap_usd": body["reserved_cost_cap_usd"],
        "shared_settings_id": SHARED_SETTINGS_ID,
        "d2l_settings_id": D2L_SETTINGS_ID,
        "evaluation_selection": evaluation_selection,
        "evaluation_selection_sha256": evaluation_selection[
            "selection_sha256"
        ],
        "evaluation_settings_template_sha256": evaluation_selection[
            "registered_option_sha256"
        ],
        "prepare_scoring": scoring_readiness["allowed"],
        "source_binding_sha256": preview["source_binding_sha256"],
        "campaign_config_sha256": preview["campaign_config_sha256"],
        "api_confirm_token": api_confirm_token,
    }
    public = {
        "schema_id": WORKFLOW_PREFLIGHT_SCHEMA_ID,
        "schema_version": WORKFLOW_APP_SCHEMA_VERSION,
        "preflight_id": preflight_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "status": "ready",
        "valid": True,
        "errors": [],
        "warnings": [
            {"code": reason, "message": reason}
            for reason in (
                blocking_reasons
                + (
                    []
                    if scoring_readiness["allowed"]
                    else [
                        "workflow_translation_only_scoring_deferred",
                        *scoring_readiness["blockers"],
                    ]
                )
            )
        ],
        "execution_mode": mode,
        "live_start_allowed": LIVE_START_ALLOWED,
        "launch_phase": "translation",
        "scoring_start_allowed": scoring_readiness["allowed"],
        "scoring_blocking_reasons": scoring_readiness["blockers"],
        "scoring_runtime": scoring_readiness["runtime"],
        "start_allowed": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "normalized_selection": {
            "schema_id": WORKFLOW_SELECTION_SCHEMA_ID,
            "schema_version": WORKFLOW_SELECTION_SCHEMA_VERSION,
            "execution_mode": mode,
            "chapter_ids": list(body["chapter_ids"]),
            "shared_option_id": SHARED_SETTINGS_ID,
            "d2l_settings_option_id": D2L_SETTINGS_ID,
            "evaluation": evaluation_selection,
            "hard_total_token_cap": body["hard_total_token_cap"],
            "reserved_cost_cap_usd": body["reserved_cost_cap_usd"],
        },
        "source_summary": setup["source_package"],
        "shared_summary": _shared_settings_option(),
        "d2l_summary": _d2l_settings_option(),
        "evaluation_summary": _evaluation_preflight_summary(
            option=evaluation_option,
            selection=evaluation_selection,
        ),
        "credential_status": credentials,
        "capability_status": _capability_status(preview),
        "bounds": _forecast_read_model(preview),
        "identities": {
            "planned_run_id": run_id,
            "workflow_run_id": identities["workflow_run_id"],
            "translation_component_run_id": identities["component_run_id"],
            "evaluation_component_run_id": identities[
                "reserved_evaluation_component_run_id"
            ],
            "source_binding_sha256": preview["source_binding_sha256"],
            "campaign_config_sha256": preview["campaign_config_sha256"],
        },
    }
    public["preflight_sha256"] = canonical_sha256(public)
    token = _issue_preflight_token(public=public, launch=launch)
    launch_read_model = {
        "script": launch["script"],
        "phase": launch["phase"],
        "preflight_id": preflight_id,
        "preflight_sha256": public["preflight_sha256"],
        "confirm_token": token,
        "planned_run_id": run_id,
        "workflow_run_id": identities["workflow_run_id"],
        "expires_at": expires_at,
    }
    return {
        **public,
        "launch": launch_read_model,
        "final_action": {
            "method": "POST",
            "href": "/api/thesis/runs",
            "required_body_schema_id": WORKFLOW_LAUNCH_SCHEMA_ID,
        },
    }


def resolve_workflow_launch(request_body: Any) -> dict[str, Any]:
    if not isinstance(request_body, Mapping):
        raise WorkflowReplayError(
            "workflow_launch_body_invalid",
            "Workflow launch body must be a JSON object.",
        )
    required = {
        "schema_id",
        "schema_version",
        "script",
        "job_id",
        "execution_mode",
        "allow_api",
        "workflow_preflight_id",
        "workflow_preflight_sha256",
        "confirm_token",
        "planned_run_id",
    }
    if set(request_body) != required:
        raise WorkflowReplayError(
            "workflow_launch_body_invalid",
            "Workflow launch confirmation has missing or unsupported fields.",
        )
    if (
        request_body["schema_id"] != WORKFLOW_LAUNCH_SCHEMA_ID
        or request_body["schema_version"] != WORKFLOW_APP_SCHEMA_VERSION
    ):
        raise WorkflowReplayError(
            "workflow_launch_schema_invalid",
            "Workflow launch confirmation schema is not supported.",
        )
    token = request_body["confirm_token"]
    if not isinstance(token, str) or not token:
        raise WorkflowReplayError(
            "workflow_preflight_token_invalid",
            "workflow_preflight_token is required.",
            403,
        )
    with _preflight_lock:
        _prune_preflight_tokens(now=time.monotonic())
        record = _preflight_tokens.get(token)
    if record is None:
        raise WorkflowReplayError(
            "workflow_preflight_token_invalid",
            "Workflow preflight token is unknown or expired.",
            403,
        )
    expected = {
        "script": record.launch["script"],
        "job_id": record.launch["job_id"],
        "execution_mode": record.launch["mode"],
        "allow_api": record.launch["allow_api"],
        "workflow_preflight_id": record.public["preflight_id"],
        "workflow_preflight_sha256": record.public["preflight_sha256"],
        "planned_run_id": record.launch["planned_run_id"],
    }
    observed = {key: request_body[key] for key in expected}
    if observed != expected:
        raise WorkflowReplayError(
            "workflow_launch_binding_invalid",
            "Workflow launch confirmation differs from the server-sealed preflight.",
            409,
        )
    if not record.public["start_allowed"]:
        reason = (
            record.public["blocking_reasons"][0]
            if record.public["blocking_reasons"]
            else "workflow_preflight_blocked"
        )
        raise WorkflowReplayError(
            reason,
            "Workflow launch is blocked by the sealed preflight.",
            403,
        )
    if record.launch["mode"] == "live":
        if (
            not LIVE_START_ALLOWED
            or not record.launch["allow_api"]
            or not record.launch["api_confirm_token"]
        ):
            raise WorkflowReplayError(
                "workflow_live_start_disabled",
                "Live workflow start is not enabled by this server build.",
                403,
            )
    return dict(record.launch)


def _issue_live_api_token(
    *,
    job_id: str,
    planned_run_id: str,
    chapter_ids: list[str],
    hard_total_token_cap: int,
    reserved_cost_cap_usd: str | None,
    preview: Mapping[str, Any],
    jobs_root: str | Path,
    tool_root: str | Path,
    python_exe: str | None,
    prepare_scoring: bool,
) -> str:
    from services.thesis_runs import (
        D2L_PROFILE_ID,
        WORKFLOW_ORCHESTRATOR_SCRIPT,
        _d2l_credential_files,
        _d2l_launch_binding_sha256,
        _evaluation_runtime_config_file,
        build_argv,
        d2l_campaign_paths,
        d2l_component_ids,
        evaluation_component_paths,
        issue_estimate_token_for_argv,
        resolve_job_db,
    )

    jobs = Path(jobs_root).resolve()
    tool = Path(tool_root).resolve()
    identities = d2l_component_ids(planned_run_id)
    paths = d2l_campaign_paths(
        jobs_root=jobs,
        job_id=job_id,
        run_id=planned_run_id,
    )
    evaluation_paths = evaluation_component_paths(
        jobs_root=jobs,
        job_id=job_id,
        workflow_run_id=identities["workflow_run_id"],
        component_run_id=identities[
            "reserved_evaluation_component_run_id"
        ],
    )
    credentials = _d2l_credential_files(required=True)
    evaluation_runtime_config = (
        _evaluation_runtime_config_file(required=True)
        if prepare_scoring
        else None
    )
    launch_binding = _d2l_launch_binding_sha256(
        job_id=job_id,
        planned_run_id=planned_run_id,
        workflow_run_id=identities["workflow_run_id"],
        component_run_id=identities["component_run_id"],
        preview=preview,
    )
    argv = build_argv(
        script=WORKFLOW_ORCHESTRATOR_SCRIPT,
        python_exe=python_exe,
        job_id=job_id,
        db=resolve_job_db(db=None, job_id=job_id, jobs_root=jobs),
        chapters=chapter_ids,
        profile=D2L_PROFILE_ID,
        allow_api=True,
        event_log=str(paths["event_log_path"]),
        run_id=planned_run_id,
        hard_total_token_cap=hard_total_token_cap,
        reserved_cost_cap_usd=reserved_cost_cap_usd,
        campaign_root=str(paths["campaign_root"]),
        job_root=str((jobs / job_id).resolve()),
        parent_root=str(
            workflow_replay_root(
                jobs_root=jobs,
                job_id=job_id,
                workflow_run_id=identities["workflow_run_id"],
            )
        ),
        workflow_run_id=identities["workflow_run_id"],
        component_run_id=identities["component_run_id"],
        evaluation_component_run_id=identities[
            "reserved_evaluation_component_run_id"
        ],
        evaluation_root=str(evaluation_paths["component_root"]),
        evaluation_runtime_root=str(evaluation_paths["runtime_root"]),
        evaluation_runtime_config=(
            None
            if evaluation_runtime_config is None
            else str(evaluation_runtime_config)
        ),
        code_root=str(tool),
        runtime_root=str(paths["runtime_root"]),
        credential_files=credentials,
    )
    return issue_estimate_token_for_argv(
        job_id=job_id,
        script=WORKFLOW_ORCHESTRATOR_SCRIPT,
        argv=argv,
        preview_kind="workflow_preflight_v1",
        run_identity_digest=launch_binding,
    )


def initialize_workflow_parent(
    *,
    jobs_root: str | Path,
    job_id: str,
    workflow_run_id: str,
    selected_chapter_ids: list[str],
    source_binding: Mapping[str, Any],
    evaluation_selection: Mapping[str, Any],
    code_commit: str,
) -> dict[str, Any]:
    from pipeline.eval.workflow_component_writer_v1 import (
        benchmark_workflow_stages_v1,
    )
    from pipeline.prepass.d2l_console_replay_contract_v1 import (
        build_stage_plan,
    )
    from pipeline.workflow_replay.relay_v1 import (
        StageDefinitionV1,
        WorkflowRelayV1,
    )
    from pipeline.workflow_replay.orchestrator_v1 import (
        WorkflowOrchestratorError,
        materialize_workflow_launch_selection_v1,
    )

    def seal_launch_selection() -> None:
        try:
            materialize_workflow_launch_selection_v1(
                root,
                evaluation_selection=evaluation_selection,
            )
        except WorkflowOrchestratorError as exc:
            raise WorkflowReplayError(
                exc.code,
                str(exc),
                409,
            ) from exc

    root = workflow_replay_root(
        jobs_root=jobs_root,
        job_id=job_id,
        workflow_run_id=workflow_run_id,
    )
    stages = []
    ordinal = 1
    for row in build_stage_plan():
        stages.append(
            StageDefinitionV1(
                stage_id=f"translation.{row['stage_id']}",
                component_id="translation",
                local_stage_id=row["stage_id"],
                order=ordinal,
                label=row["label"],
                producer=row["producer"],
            )
        )
        ordinal += 1
    for row in benchmark_workflow_stages_v1(selected_chapter_ids):
        local_stage_id = row["stage_id"]
        label = (
            "Evaluation Preflight"
            if local_stage_id == "preflight"
            else (
                "Evaluation Aggregation"
                if local_stage_id == "aggregation"
                else f"Evaluate {local_stage_id.removeprefix('chapter_')}"
            )
        )
        stages.append(
            StageDefinitionV1(
                stage_id=f"evaluation.{local_stage_id}",
                component_id="evaluation",
                local_stage_id=local_stage_id,
                order=ordinal,
                label=label,
                producer=row["agent"],
            )
        )
        ordinal += 1
    stages.append(
        StageDefinitionV1(
            stage_id="publication.export",
            component_id="publication",
            local_stage_id="export",
            order=ordinal,
            label="Publication Export",
            producer="publication_exporter",
        )
    )

    if root.exists():
        try:
            manifest = validate_workflow_parent_package_v1(root)
        except WorkflowReplayContractError as exc:
            raise WorkflowReplayError(
                "workflow_parent_invalid",
                f"Existing parent workflow package failed validation: {exc}",
                409,
            ) from exc
        if (
            manifest["workflow_run_id"] != workflow_run_id
            or manifest["job_id"] != job_id
            or manifest["source_package_bindings"]
            != _parent_source_bindings(source_binding)
        ):
            raise WorkflowReplayError(
                "workflow_parent_identity_drift",
                "Existing parent workflow package has a different sealed identity.",
                409,
            )
        seal_launch_selection()
        return manifest

    try:
        relay = WorkflowRelayV1(
            root,
            workflow_run_id=workflow_run_id,
            job_id=job_id,
            source_package_bindings=_parent_source_bindings(source_binding),
            stages=tuple(stages),
            code_commit=code_commit,
        )
        manifest = relay.validate_parent_package()
        seal_launch_selection()
        return manifest
    except WorkflowReplayContractError as exc:
        raise WorkflowReplayError(
            "workflow_parent_initialization_failed",
            f"Parent workflow package could not be initialized: {exc}",
            409,
        ) from exc


def _issue_preflight_token(
    *,
    public: dict[str, Any],
    launch: dict[str, Any],
) -> str:
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    record = _WorkflowPreflightToken(
        token=token,
        expires_at=now + PREFLIGHT_TTL_SECONDS,
        public=dict(public),
        launch=dict(launch),
    )
    with _preflight_lock:
        _prune_preflight_tokens(now=now)
        while len(_preflight_tokens) >= MAX_ACTIVE_PREFLIGHTS:
            oldest = next(iter(_preflight_tokens))
            _preflight_tokens.pop(oldest, None)
        _preflight_tokens[token] = record
    return token


def _prune_preflight_tokens(*, now: float) -> None:
    expired = [
        token
        for token, record in _preflight_tokens.items()
        if record.expires_at <= now
    ]
    for token in expired:
        _preflight_tokens.pop(token, None)


def _validate_preflight_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowReplayError(
            "workflow_preflight_body_invalid",
            "Workflow preflight body must be a JSON object.",
        )
    required = {
        "schema_id",
        "schema_version",
        "execution_mode",
        "chapter_ids",
        "shared_option_id",
        "d2l_settings_option_id",
        "evaluation",
        "hard_total_token_cap",
        "reserved_cost_cap_usd",
    }
    if set(value) != required:
        raise WorkflowReplayError(
            "workflow_preflight_body_invalid",
            "Workflow preflight body has missing or unsupported fields.",
        )
    if (
        value["schema_id"] != WORKFLOW_SELECTION_SCHEMA_ID
        or value["schema_version"] != WORKFLOW_SELECTION_SCHEMA_VERSION
    ):
        raise WorkflowReplayError(
            "workflow_preflight_schema_invalid",
            "Workflow setup selection schema is not supported.",
        )
    mode = value["execution_mode"]
    if mode not in {"dry_run", "live"}:
        raise WorkflowReplayError(
            "workflow_mode_invalid",
            "mode must be dry_run or live.",
        )
    chapter_ids = value["chapter_ids"]
    if not isinstance(chapter_ids, list) or any(
        not isinstance(item, str) or not item for item in chapter_ids
    ):
        raise WorkflowReplayError(
            "workflow_chapters_invalid",
            "chapter_ids must be an array of non-empty server-advertised IDs.",
        )
    token_cap = value["hard_total_token_cap"]
    if token_cap is None:
        token_cap = 6_000_000
    if (
        isinstance(token_cap, bool)
        or not isinstance(token_cap, int)
        or token_cap <= 0
        or token_cap > 20_000_000
    ):
        raise WorkflowReplayError(
            "workflow_token_cap_invalid",
            "hard_total_token_cap must be an integer from 1 to 20000000.",
        )
    evaluation = _validate_evaluation_selection_request(value["evaluation"])
    return {
        "execution_mode": mode,
        "chapter_ids": list(chapter_ids),
        "shared_option_id": _required_string(
            value["shared_option_id"], "shared_option_id"
        ),
        "d2l_settings_option_id": _required_string(
            value["d2l_settings_option_id"], "d2l_settings_option_id"
        ),
        "evaluation": evaluation,
        "hard_total_token_cap": token_cap,
        "reserved_cost_cap_usd": _nullable_positive_decimal(
            value["reserved_cost_cap_usd"]
        ),
    }


def _validate_evaluation_selection_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowReplayError(
            "workflow_evaluation_selection_invalid",
            "evaluation must be a JSON object.",
        )
    required = {
        "settings_option_id",
        "selected_chapter_ids",
        "selected_arm_ids",
        "selected_scorer_ids",
        "highlight_pair",
    }
    if set(value) != required:
        raise WorkflowReplayError(
            "workflow_evaluation_selection_invalid",
            "evaluation has missing or unsupported fields.",
        )
    selected_chapter_ids = _string_id_list(
        value["selected_chapter_ids"],
        field="evaluation.selected_chapter_ids",
    )
    selected_arm_ids = _string_id_list(
        value["selected_arm_ids"],
        field="evaluation.selected_arm_ids",
    )
    selected_scorer_ids = _string_id_list(
        value["selected_scorer_ids"],
        field="evaluation.selected_scorer_ids",
    )
    highlight = value["highlight_pair"]
    if highlight is not None:
        if (
            not isinstance(highlight, Mapping)
            or set(highlight)
            != {"baseline_arm_id", "candidate_arm_id"}
        ):
            raise WorkflowReplayError(
                "workflow_highlight_pair_invalid",
                "evaluation.highlight_pair must name baseline_arm_id and candidate_arm_id.",
            )
        baseline = highlight["baseline_arm_id"]
        candidate = highlight["candidate_arm_id"]
        if (
            not isinstance(baseline, str)
            or not baseline
            or not isinstance(candidate, str)
            or not candidate
            or baseline == candidate
        ):
            raise WorkflowReplayError(
                "workflow_highlight_pair_invalid",
                "evaluation.highlight_pair must contain two distinct IDs.",
            )
    return {
        "settings_option_id": _required_string(
            value["settings_option_id"],
            "evaluation.settings_option_id",
        ),
        "selected_chapter_ids": selected_chapter_ids,
        "selected_arm_ids": selected_arm_ids,
        "selected_scorer_ids": selected_scorer_ids,
        "highlight_pair": None if highlight is None else dict(highlight),
    }


def _normalize_evaluation_selection(
    value: Mapping[str, Any],
    *,
    parent_chapter_ids: list[str],
    option: Mapping[str, Any],
) -> dict[str, Any]:
    catalog = option["selection_catalog"]
    chapters = list(value["selected_chapter_ids"])
    arms = list(value["selected_arm_ids"])
    scorers = list(value["selected_scorer_ids"])
    if not _is_ordered_subset(
        chapters,
        allowed=catalog["chapter_ids"],
        minimum=int(option["constraints"]["minimum_chapter_count"]),
    ) or not set(chapters).issubset(parent_chapter_ids):
        raise WorkflowReplayError(
            "workflow_evaluation_chapters_invalid",
            "Evaluation chapters must be a canonical subset of the selected workflow chapters.",
        )
    if not _is_ordered_subset(
        arms,
        allowed=catalog["arm_ids"],
        minimum=int(option["constraints"]["minimum_arm_count"]),
    ):
        raise WorkflowReplayError(
            "workflow_evaluation_arms_invalid",
            "Evaluation arms must be a canonical registered subset with at least two arms.",
        )
    if not _is_ordered_subset(
        scorers,
        allowed=catalog["scorer_ids"],
        minimum=int(option["constraints"]["minimum_scorer_count"]),
    ):
        raise WorkflowReplayError(
            "workflow_evaluation_scorers_invalid",
            "Evaluation scorers must be a canonical registered non-empty subset.",
        )
    highlight = value["highlight_pair"]
    if highlight is not None and (
        highlight["baseline_arm_id"] not in arms
        or highlight["candidate_arm_id"] not in arms
    ):
        raise WorkflowReplayError(
            "workflow_highlight_pair_invalid",
            "evaluation.highlight_pair must use two selected arms.",
        )
    basis = {
        "settings_option_id": option["option_id"],
        "selected_chapter_ids": chapters,
        "selected_arm_ids": arms,
        "selected_scorer_ids": scorers,
        "highlight_pair": None if highlight is None else dict(highlight),
        "registered_option_sha256": option["sha256"],
    }
    return {
        **basis,
        "selection_sha256": canonical_sha256(basis),
    }


def _evaluation_preflight_summary(
    *,
    option: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_id": EVALUATION_SETTINGS_SCHEMA_ID,
        "schema_version": EVALUATION_SETTINGS_SCHEMA_VERSION,
        "settings_status": "pending_scoring_handoff",
        "settings_sha256": None,
        "selection_sha256": selection["selection_sha256"],
        "registered_option_sha256": option["sha256"],
        "settings_option_id": option["option_id"],
        "selected_chapter_ids": list(selection["selected_chapter_ids"]),
        "selected_arm_ids": list(selection["selected_arm_ids"]),
        "selected_scorer_ids": list(selection["selected_scorer_ids"]),
        "highlight_pair": selection["highlight_pair"],
        "registered_authority": dict(
            option["fixed_facts"]["registered_authority"]
        ),
    }


def _string_id_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise WorkflowReplayError(
            "workflow_evaluation_selection_invalid",
            f"{field} must be an array of non-empty IDs.",
        )
    return list(value)


def _is_ordered_subset(
    values: list[str],
    *,
    allowed: list[str] | tuple[str, ...],
    minimum: int,
) -> bool:
    if len(values) < minimum or len(values) != len(set(values)):
        return False
    selected = set(values)
    return values == [item for item in allowed if item in selected]


def _load_setup_project(
    doc_id: str,
    *,
    jobs_root: str | Path,
) -> tuple[dict[str, Any], Any]:
    from pipeline.prepass.d2l_project_campaign_v2 import load_project
    from services.project_runtime import get_project_runtime_status

    try:
        status = get_project_runtime_status(doc_id, jobs_root=jobs_root)
    except Exception as exc:
        code = getattr(exc, "code", "workflow_project_invalid")
        status_code = int(getattr(exc, "status", 409))
        raise WorkflowReplayError(code, str(exc), status_code) from exc
    if not status.get("managed"):
        raise WorkflowReplayError(
            "workflow_managed_source_required",
            "Workflow setup requires a managed Canonical Source Package.",
            409,
        )
    if status.get("lifecycle") != "finalized_pre_run":
        raise WorkflowReplayError(
            "workflow_source_not_finalized",
            "Workflow setup requires lifecycle finalized_pre_run.",
            409,
        )
    if not status.get("prepared"):
        raise WorkflowReplayError(
            "workflow_runtime_not_prepared",
            "Workflow setup requires a prepared managed runtime.",
            409,
        )
    try:
        project = load_project(
            Path(jobs_root).resolve() / status["job_id"],
            verify_tree=True,
        )
    except Exception as exc:
        raise WorkflowReplayError(
            "workflow_source_package_invalid",
            f"Prepared source package failed validation: {exc}",
            409,
        ) from exc
    return status, project


def _parent_source_bindings(source_binding: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": role, "binding": dict(source_binding[role])}
        for role in (
            "document",
            "structure_manifest",
            "asset_manifest",
            "admitted_projection",
            "normalization_receipt",
            "package_seal",
        )
    ]


def _shared_settings_option() -> dict[str, Any]:
    from pipeline.prepass.d2l_project_campaign_v2 import TRANSPORT_RETRY_POLICY

    fixed_facts = {
        "sources": [
            {
                "source_id": "shopaikey_gemini_proxy_v2",
                "source_revision": "shopaikey_gemini_profile_v2",
                "requested_model_id": "gemini-3.5-flash",
                "output_mode": "prompt_json",
                "capability_status": "qualified",
                "credential_ref": "credential.shopaikey_gemini_proxy_v1",
            },
            {
                "source_id": "modelapi_shared_v1",
                "source_revision": "modelapi_profile_v1",
                "requested_model_id": "gpt-5.4",
                "output_mode": "prompt_json",
                "capability_status": "qualified",
                "credential_ref": "credential.modelapi_shared_v1",
            },
            {
                "source_id": "modelapi_shared_v1",
                "source_revision": "modelapi_profile_v1",
                "requested_model_id": "gpt-5.5",
                "output_mode": "prompt_json",
                "capability_status": "qualified",
                "credential_ref": "credential.modelapi_shared_v1",
            },
        ],
        "policy": {
            "transport_retry_cap": int(TRANSPORT_RETRY_POLICY["max_retries"]),
            "fallback_enabled": False,
            "rotation_enabled": False,
            "concurrency": 1,
            "cache_policy": "exact_local_cache_v1",
            "semantic_retry_policy": "role_sealed_bounded_v1",
            "tariff_status": "unpinned",
        },
    }
    return {
        "option_id": SHARED_SETTINGS_ID,
        "label": "Registered shared API profile",
        "revision": "v2_transport_retry",
        "sha256": canonical_sha256(fixed_facts),
        "enabled": True,
        "status": "registered",
        "credential_status": _credential_status(),
        "capability_status": [
            {
                "source_id": row["source_id"],
                "requested_model_id": row["requested_model_id"],
                "status": row["capability_status"],
            }
            for row in fixed_facts["sources"]
        ],
        "fixed_facts": fixed_facts,
        "constraints": {
            "server_owned": True,
            "arbitrary_source_model_output_forbidden": True,
        },
    }


def _d2l_settings_option() -> dict[str, Any]:
    fixed_facts = {
        "profile_id": "technical_d2l_v1",
        "arm_ids": ["s0", "s1"],
        "stages": [
            "preflight",
            "b1_candidate_discovery",
            "candidate_index",
            "b2_admission_translation",
            "auditor_morphology",
            "auditor_target_collision",
            "auditor_multi_target",
            "glossary_seal",
            "translator",
            "translation_quality_audit",
            "scoring_handoff_fragment",
        ],
    }
    return {
        "option_id": D2L_SETTINGS_ID,
        "label": "Technical D2L workflow",
        "revision": "v1",
        "sha256": canonical_sha256(fixed_facts),
        "enabled": True,
        "status": "registered",
        "fixed_facts": fixed_facts,
        "constraints": {"server_owned": True},
    }


def _evaluation_settings_option(
    *,
    available_chapter_ids: list[str] | None = None,
    job_root: Path | None = None,
    job_id: str | None = None,
    source_binding_sha256: str | None = None,
) -> dict[str, Any]:
    available_order = (
        list(EVALUATION_CHAPTER_ORDER)
        if available_chapter_ids is None
        else list(dict.fromkeys(available_chapter_ids))
    )
    available = set(available_order)
    registered_default_chapters = [
        chapter_id
        for chapter_id in EVALUATION_CHAPTER_ORDER
        if chapter_id in available
    ]
    catalog_chapters = list(available_order)
    default_chapters = list(catalog_chapters)
    option_status = "pending_baseline_registration"
    fixed_facts: dict[str, Any] = {
        "settings_schema_id": EVALUATION_SETTINGS_SCHEMA_ID,
        "settings_schema_version": EVALUATION_SETTINGS_SCHEMA_VERSION,
        "arm_ids": list(EVALUATION_ARM_ORDER),
        "scorer_ids": list(EVALUATION_SCORER_ORDER),
        "aggregation_policy": "evaluation_benchmark_aggregation_v1",
        "report_policy": "full_run_report_v1",
        "verdict_policy": "evaluation_verdict_v1",
        "scoring_handoff_authority": (
            "exact_ordered_five_arm_scoring_handoff_v1"
        ),
        "registered_authority": {
            "status": "pending_runtime_registration",
            "benchmark_preset_ref": None,
            "evaluation_config_ref": None,
            "scorer_set_ref": None,
            "evaluation_profile_ref": None,
            "policy_profile_ref": None,
            "shared_selection_ref": None,
        },
    }
    option_sha256 = canonical_sha256(fixed_facts)
    if (
        job_root is not None
        and job_id is not None
        and source_binding_sha256 is not None
        and (Path(job_root) / "workflow_runtime_v1.json").is_file()
    ):
        from pipeline.eval.workflow_runtime_bundle_v1 import (
            load_workflow_scoring_baseline_template_from_workflow_runtime_v1,
        )
        from pipeline.eval.workflow_runtime_factory_v1 import (
            build_evaluation_registered_option_facts_v1,
        )

        try:
            loaded = (
                load_workflow_scoring_baseline_template_from_workflow_runtime_v1(
                    Path(job_root),
                    expected_job_id=job_id,
                    expected_source_binding_sha256=source_binding_sha256,
                    selected_chapter_ids=registered_default_chapters,
                )
            )
            registered = loaded.registered_option
            if registered["settings_option_id"] != EVALUATION_SETTINGS_ID:
                raise ValueError("registered settings option ID drifted")
            fixed_facts = build_evaluation_registered_option_facts_v1(
                loaded.settings_authority,
                evaluation_profile_ref=registered[
                    "evaluation_profile_ref"
                ]["artifact_ref"],
                policy_profile_ref=(
                    None
                    if registered["policy_profile_ref"] is None
                    else registered["policy_profile_ref"]["artifact_ref"]
                ),
                shared_selection_ref=registered[
                    "shared_selection_ref"
                ]["artifact_ref"],
            )
            option_sha256 = canonical_sha256(fixed_facts)
            if option_sha256 != registered["registered_option_sha256"]:
                raise ValueError("registered Evaluation option hash drifted")
            catalog_chapters = [
                chapter_id
                for chapter_id in loaded.settings_authority.chapter_ids
                if chapter_id in available
            ]
            default_chapters = list(catalog_chapters)
            option_status = "registered"
        except Exception as exc:
            raise WorkflowReplayError(
                "workflow_evaluation_runtime_invalid",
                f"Registered Evaluation runtime is invalid: {exc}",
                409,
            ) from exc
    return {
        "option_id": EVALUATION_SETTINGS_ID,
        "label": "Registered Evaluation workflow",
        "revision": EVALUATION_SETTINGS_SCHEMA_VERSION,
        "sha256": option_sha256,
        "enabled": bool(default_chapters),
        "status": option_status,
        "fixed_facts": fixed_facts,
        "selection_catalog": {
            "chapter_ids": list(catalog_chapters),
            "arm_ids": list(EVALUATION_ARM_ORDER),
            "scorer_ids": list(EVALUATION_SCORER_ORDER),
        },
        "default_selection": {
            "settings_option_id": EVALUATION_SETTINGS_ID,
            "selected_chapter_ids": default_chapters,
            "selected_arm_ids": list(EVALUATION_ARM_ORDER),
            "selected_scorer_ids": list(EVALUATION_SCORER_ORDER),
            "highlight_pair": None,
        },
        "constraints": {
            "server_owned": True,
            "minimum_chapter_count": 1,
            "minimum_arm_count": 2,
            "minimum_scorer_count": 1,
            "canonical_order_required": True,
        },
    }


def _credential_status() -> list[dict[str, str]]:
    result = []
    for credential_ref, env_name in _CREDENTIAL_BINDINGS:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            status = "missing"
        else:
            path = Path(raw).expanduser()
            status = (
                "available"
                if path.is_absolute() and path.is_file() and not path.is_symlink()
                else "invalid"
            )
        result.append(
            {
                "credential_ref": credential_ref,
                "status": status,
            }
        )
    return result


def _capability_status(preview: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    sources = {
        row["source_id"]: row for row in preview["transport_sources"]
    }
    for role in preview["semantic_roles"]:
        source = sources[role["source_id"]]
        result.append(
            {
                "role_id": role["role_id"],
                "source_id": role["source_id"],
                "source_revision": source["source_revision"],
                "requested_model_id": role["model_id"],
                "output_mode": source["output_mode"],
                "status": "qualified",
            }
        )
    return result


def _forecast_read_model(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: preview.get(key)
        for key in (
            "selected_block_count",
            "chapter_counts",
            "channel_counts",
            "window_counts",
            "forecast_total_tokens",
            "forecast_token_range",
            "forecast_status",
            "hard_total_token_cap",
            "theoretical_role_reserve_tokens",
            "hard_physical_attempt_cap",
            "reserved_cost_cap_usd",
            "cost_usd",
            "cost_basis",
        )
    }


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise WorkflowReplayError(
            "workflow_preflight_body_invalid",
            f"{name} must be a non-empty string.",
        )
    return value


def _nullable_positive_decimal(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise WorkflowReplayError(
            "workflow_cost_cap_invalid",
            "reserved_cost_cap_usd must be a positive decimal or null.",
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise WorkflowReplayError(
            "workflow_cost_cap_invalid",
            "reserved_cost_cap_usd must be a positive decimal or null.",
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise WorkflowReplayError(
            "workflow_cost_cap_invalid",
            "reserved_cost_cap_usd must be a positive decimal or null.",
        )
    return format(parsed.normalize(), "f")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def workflow_replay_root(
    *,
    jobs_root: str | Path,
    job_id: str,
    workflow_run_id: str,
) -> Path:
    job = validate_job_id(job_id, required=True)
    workflow = validate_run_id(workflow_run_id, required=True)
    root = Path(jobs_root).resolve()
    candidate = (root / "_work" / "workflow_replay" / job / workflow).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkflowReplayError(
            "workflow_replay_path_invalid",
            "Resolved workflow replay root is outside THESIS_JOBS_ROOT.",
            500,
        ) from exc
    return candidate


def read_workflow_replay(
    entry: Mapping[str, Any],
    *,
    jobs_root: str | Path,
    after_seq: int = 0,
    wait_ms: int = 0,
) -> dict[str, Any]:
    if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
        raise WorkflowReplayError(
            "workflow_replay_cursor_invalid",
            "after_seq must be a non-negative integer.",
        )
    if isinstance(wait_ms, bool) or not isinstance(wait_ms, int):
        raise WorkflowReplayError(
            "workflow_replay_wait_invalid",
            "wait_ms must be an integer.",
        )
    if wait_ms < 0 or wait_ms > MAX_WAIT_MS:
        raise WorkflowReplayError(
            "workflow_replay_wait_invalid",
            f"wait_ms must be between 0 and {MAX_WAIT_MS}.",
        )

    root = _root_for_entry(entry, jobs_root=jobs_root)
    package = _load_validated_parent(root, entry=entry)
    latest_seq = package["manifest"]["latest_event_seq"]
    terminal = package["manifest"]["status"] in {"failed", "succeeded"}
    if latest_seq > after_seq or terminal or wait_ms == 0:
        return _read_envelope(
            entry,
            root=root,
            package=package,
            after_seq=after_seq,
            jobs_root=Path(jobs_root).resolve(),
        )

    manifest_commit_sha256 = _manifest_commit_sha256(root)
    deadline = time.monotonic() + (wait_ms / 1000)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _read_envelope(
                entry,
                root=root,
                package=package,
                after_seq=after_seq,
                jobs_root=Path(jobs_root).resolve(),
            )
        time.sleep(min(0.1, remaining))
        current_manifest_sha256 = _manifest_commit_sha256(root)
        if current_manifest_sha256 == manifest_commit_sha256:
            continue
        package = _load_validated_parent(root, entry=entry)
        latest_seq = package["manifest"]["latest_event_seq"]
        terminal = package["manifest"]["status"] in {"failed", "succeeded"}
        manifest_commit_sha256 = current_manifest_sha256
        if latest_seq > after_seq or terminal:
            return _read_envelope(
                entry,
                root=root,
                package=package,
                after_seq=after_seq,
                jobs_root=Path(jobs_root).resolve(),
            )


def read_workflow_live_console(
    entry: Mapping[str, Any],
    *,
    jobs_root: str | Path,
) -> dict[str, Any]:
    """Read a bounded presentation snapshot without validating the full package.

    This path is intentionally display-only. Mutating actions, artifact reads,
    Resume and final Replay continue to use the authoritative validators.
    """

    root = _root_for_entry(entry, jobs_root=jobs_root)
    manifest_path = root / "workflow_manifest.json"
    events_path = root / "events.jsonl"
    cache_key = _live_console_file_key(manifest_path, events_path)
    cache_id = str(root)
    with _live_console_cache_lock:
        cached = _live_console_cache.get(cache_id)
        if cached is not None and cached[0] == cache_key:
            return cached[1]

    try:
        manifest = _read_json(manifest_path)
        raw_events, tail_truncated = _read_regular_tail_bytes(
            events_path,
            max_bytes=LIVE_CONSOLE_TAIL_BYTES,
        )
        if _live_console_file_key(manifest_path, events_path) != cache_key:
            raise WorkflowReplayError(
                "workflow_live_snapshot_changed",
                "Live Console snapshot changed while it was being read.",
                409,
            )
        result = _build_workflow_live_console(
            entry,
            manifest=manifest,
            raw_events=raw_events,
            tail_truncated=tail_truncated,
        )
    except (OSError, WorkflowReplayContractError, WorkflowReplayError) as exc:
        with _live_console_cache_lock:
            cached = _live_console_cache.get(cache_id)
            if cached is not None:
                return cached[1]
        if isinstance(exc, WorkflowReplayError):
            raise
        raise WorkflowReplayError(
            "workflow_live_snapshot_unavailable",
            f"Live Console snapshot is temporarily unavailable: {exc}",
            409,
        ) from exc

    with _live_console_cache_lock:
        _live_console_cache[cache_id] = (cache_key, result)
        if len(_live_console_cache) > 8:
            oldest = next(iter(_live_console_cache))
            if oldest != cache_id:
                _live_console_cache.pop(oldest, None)
    return result


def _live_console_file_key(
    manifest_path: Path,
    events_path: Path,
) -> tuple[int, int, int, int]:
    manifest_stat = manifest_path.stat()
    events_stat = events_path.stat()
    return (
        manifest_stat.st_mtime_ns,
        manifest_stat.st_size,
        events_stat.st_mtime_ns,
        events_stat.st_size,
    )


def _build_workflow_live_console(
    entry: Mapping[str, Any],
    *,
    manifest: Any,
    raw_events: bytes,
    tail_truncated: bool,
) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise WorkflowReplayError(
            "workflow_live_manifest_invalid",
            "Live Console manifest is not an object.",
            409,
        )
    workflow_run_id = str(entry.get("workflow_run_id") or "")
    job_id = str(entry.get("job_id") or "")
    if (
        manifest.get("workflow_run_id") != workflow_run_id
        or manifest.get("job_id") != job_id
    ):
        raise WorkflowReplayError(
            "workflow_live_identity_drift",
            "Live Console manifest identity differs from the run registry.",
            409,
        )
    latest_seq = manifest.get("latest_event_seq")
    if isinstance(latest_seq, bool) or not isinstance(latest_seq, int) or latest_seq < 0:
        raise WorkflowReplayError(
            "workflow_live_manifest_invalid",
            "Live Console manifest has no valid event cursor.",
            409,
        )

    visible_events: deque[dict[str, Any]] = deque(maxlen=LIVE_CONSOLE_EVENT_LIMIT)
    latest_usage: Mapping[str, Any] | None = None
    observed_seq = 0
    first_seq = 0
    for line in raw_events.splitlines():
        if not line:
            raise WorkflowReplayError(
                "workflow_live_event_invalid",
                "Live Console event stream contains a blank row.",
                409,
            )
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowReplayError(
                "workflow_live_event_invalid",
                "Live Console event stream contains an incomplete JSON row.",
                409,
            ) from exc
        seq = event.get("seq") if isinstance(event, Mapping) else None
        if (
            isinstance(seq, bool)
            or not isinstance(seq, int)
            or seq <= 0
            or event.get("workflow_run_id") != workflow_run_id
            or (observed_seq and seq != observed_seq + 1)
        ):
            raise WorkflowReplayError(
                "workflow_live_event_invalid",
                "Live Console event identity or sequence is inconsistent.",
                409,
            )
        if first_seq == 0:
            first_seq = seq
        observed_seq = seq
        event_name = event.get("event")
        if (
            event_name == "usage_snapshot"
            and (event.get("component") or {}).get("component_id") == "translation"
        ):
            latest_usage = event
            continue
        if event_name == "term_lifecycle":
            continue
        visible_events.append(_compact_live_console_event(event))

    if observed_seq != latest_seq:
        raise WorkflowReplayError(
            "workflow_live_event_exact_cover",
            "Live Console manifest and event cursor are between publications.",
            409,
        )
    status = str(manifest.get("status") or "running").lower()
    source_mode = "replay" if status in {"failed", "succeeded"} else "live"
    resume = manifest.get("resume") if isinstance(manifest.get("resume"), Mapping) else {}
    events = list(visible_events)
    return {
        "schema": WORKFLOW_LIVE_CONSOLE_SCHEMA,
        "presentation_only": True,
        "run_id": entry["run_id"],
        "workflow_run_id": workflow_run_id,
        "job_id": job_id,
        "manifest": dict(manifest),
        "events": events,
        "usage": _live_console_usage(latest_usage, workflow_run_id),
        "source_mode": source_mode,
        "cursor": {
            "window_start_seq": events[0]["seq"] if events else latest_seq,
            "through_seq": latest_seq,
            "latest_seq": latest_seq,
            "terminal": status in {"failed", "succeeded"},
            "truncated": tail_truncated or first_seq > 1,
        },
        "actions": {
            "pause": {
                "allowed": source_mode == "live" and status == "running",
                "method": "POST",
                "href": f"/api/thesis/runs/{entry['run_id']}/pause",
            },
            "resume": {
                "allowed": source_mode == "live" and bool(resume.get("available")),
                "method": "POST",
                "href": f"/api/thesis/runs/{entry['run_id']}/resume",
                "component_id": resume.get("component_id"),
            },
            "replay": {
                "allowed": True,
                "method": "GET",
                "href": f"/api/thesis/runs/{entry['run_id']}/workflow-replay?after_seq=0&wait_ms=0",
            },
            "score": {
                "allowed": False,
                "blocking_reasons": ["presentation_snapshot_not_scoring_authority"],
            },
        },
    }


def _compact_live_console_event(event: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(event)
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return row
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= LIVE_CONSOLE_PAYLOAD_LIMIT:
        return row
    keep = {
        "progress",
        "completed",
        "total",
        "unit",
        "work_id",
        "work_kind",
        "current_work_id",
        "validator_id",
        "subject_ref",
        "reason_codes",
        "retryable",
        "retry_kind",
        "retry_count",
        "outcome",
        "index",
        "max",
        "logical_request_id",
        "checkpoint_ref",
        "checkpoint_sha256",
        "paused_reason",
        "resume_available",
        "artifact_ref",
        "artifact_kind",
        "schema_version",
        "sha256",
        "sha256_kind",
        "stage_id",
        "status",
        "exit_code",
    }
    row["payload"] = {
        key: value for key, value in payload.items() if key in keep
    }
    row["payload"]["presentation_details_omitted"] = True
    return row


def _live_console_usage(
    event: Mapping[str, Any] | None,
    workflow_run_id: str,
) -> dict[str, Any] | None:
    if event is None:
        return None
    payload = event.get("payload")
    component = event.get("component")
    if not isinstance(payload, Mapping) or not isinstance(component, Mapping):
        return None
    totals = payload.get("component_cumulative")
    if not isinstance(totals, Mapping):
        return None
    cache = totals.get("cache_counters")
    cache = cache if isinstance(cache, Mapping) else {}
    total = {
        "component_id": "translation",
        "component_run_id": payload.get("component_run_id"),
        "component_attempt_id": payload.get("component_attempt_id"),
        "component_attempt_index": payload.get("component_attempt_id"),
        "stage_id": None,
        "snapshot_seq": payload.get("snapshot_seq"),
        "accepted_through_component_seq": component.get("component_seq"),
        "physical_call_count": totals.get("physical_attempt_count"),
        "cache_observation_count": totals.get("cache_observation_count"),
        "prompt_tokens": totals.get("prompt_tokens"),
        "completion_tokens": totals.get("completion_tokens"),
        "reasoning_tokens": totals.get("reasoning_tokens"),
        "cached_input_tokens": totals.get("cached_input_tokens"),
        "total_tokens": totals.get("total_tokens"),
        "cache_hit_count": cache.get("hit", 0),
        "cache_miss_count": cache.get("miss", 0),
        "unknown_attempt_count": 0,
        "cost_status": totals.get("cost_status"),
        "cost_usd": totals.get("cost_usd"),
        "currency": totals.get("currency"),
        "snapshot_sha256": payload.get("snapshot_sha256"),
    }
    return {
        "schema_id": "WorkflowUsageReadModelV1",
        "schema_version": "1.0.0",
        "workflow_run_id": workflow_run_id,
        "validated": True,
        "calls": [],
        "stage_totals": [],
        "component_totals": [total],
        "workflow_total": None,
        "validation": {
            "valid": True,
            "authority": "producer_snapshots_and_neutral_relay",
            "presentation_only": True,
        },
    }
def read_workflow_artifact(
    entry: Mapping[str, Any],
    *,
    jobs_root: str | Path,
    artifact_ref: str,
) -> dict[str, Any]:
    root = _root_for_entry(entry, jobs_root=jobs_root)
    package = _load_validated_parent(root, entry=entry)
    artifact = _artifact_by_ref(package["artifact_index"], artifact_ref)
    path = _resolve_artifact(root, artifact_ref)
    raw = _read_regular_bytes(path, max_bytes=MAX_ARTIFACT_BYTES)
    if physical_sha256(raw) != artifact["imported_physical_sha256"]:
        raise WorkflowReplayError(
            "workflow_artifact_hash_drift",
            "Indexed workflow artifact bytes no longer match their accepted receipt.",
            409,
        )

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return {
        "schema": WORKFLOW_ARTIFACT_READ_SCHEMA,
        "run_id": entry["run_id"],
        "workflow_run_id": entry["workflow_run_id"],
        "artifact": artifact,
        "media_type": media_type,
        "filename": path.name,
        "content": raw,
    }


def _root_for_entry(
    entry: Mapping[str, Any],
    *,
    jobs_root: str | Path,
) -> Path:
    run_id = validate_run_id(entry.get("run_id"), required=True)
    job_id = validate_job_id(entry.get("job_id"), required=True)
    workflow_run_id = validate_run_id(
        entry.get("workflow_run_id"), required=True
    )
    if run_id is None or job_id is None or workflow_run_id is None:
        raise WorkflowReplayError(
            "workflow_replay_identity_missing",
            "Run registry entry does not bind a workflow replay identity.",
            409,
        )
    root = workflow_replay_root(
        jobs_root=jobs_root,
        job_id=job_id,
        workflow_run_id=workflow_run_id,
    )
    if not root.is_dir() or root.is_symlink():
        raise WorkflowReplayError(
            "workflow_replay_not_ready",
            "Validated parent workflow replay package is not available.",
            409,
        )
    return root


def _load_validated_parent(
    root: Path,
    *,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        _assert_parent_document_bounds(root)
        manifest = validate_workflow_parent_package_v1(root)
        artifact_index = validate_workflow_artifact_index_v1(
            _read_json(root / "artifact_index.json")
        )
        events = _read_events(root / "events.jsonl")
    except WorkflowReplayContractError as exc:
        raise WorkflowReplayError(
            "workflow_replay_invalid",
            f"Parent workflow replay package failed validation: {exc}",
            409,
        ) from exc

    expected = {
        "workflow_run_id": entry.get("workflow_run_id"),
        "job_id": entry.get("job_id"),
    }
    observed = {
        "workflow_run_id": manifest["workflow_run_id"],
        "job_id": manifest["job_id"],
    }
    if observed != expected:
        raise WorkflowReplayError(
            "workflow_replay_identity_drift",
            "Parent workflow replay identity differs from the run registry.",
            409,
        )
    if artifact_index["workflow_run_id"] != manifest["workflow_run_id"]:
        raise WorkflowReplayError(
            "workflow_replay_artifact_identity_drift",
            "Parent artifact index belongs to a different workflow.",
            409,
        )
    if len(events) != manifest["latest_event_seq"]:
        raise WorkflowReplayError(
            "workflow_replay_event_exact_cover",
            "Parent event stream does not exact-cover the manifest cursor.",
            409,
        )
    return {
        "manifest": manifest,
        "artifact_index": artifact_index,
        "events": events,
    }


def _read_envelope(
    entry: Mapping[str, Any],
    *,
    root: Path,
    package: Mapping[str, Any],
    after_seq: int,
    jobs_root: Path,
) -> dict[str, Any]:
    manifest = package["manifest"]
    events = [event for event in package["events"] if event["seq"] > after_seq]
    typed_artifacts = _typed_artifacts(root, package["artifact_index"])
    artifact_bodies = {
        item["binding"]["artifact_ref"]: item["body"]
        for item in typed_artifacts
    }
    validated_artifacts = _validated_artifacts(typed_artifacts)
    latest_seq = manifest["latest_event_seq"]
    source_mode = (
        "replay"
        if manifest["status"] in {"failed", "succeeded"}
        else "live"
    )
    evaluation_scope = _evaluation_scope_read_model(
        entry,
        typed_artifacts=typed_artifacts,
    )
    evaluation_console = _evaluation_console_read_model(
        manifest=manifest,
        events=package["events"],
        artifact_index=package["artifact_index"],
        typed_artifacts=typed_artifacts,
    )
    returned_through = events[-1]["seq"] if events else after_seq
    if after_seq > latest_seq:
        raise WorkflowReplayError(
            "workflow_replay_cursor_ahead",
            "after_seq is ahead of the accepted parent event stream.",
            409,
        )
    return {
        "schema": WORKFLOW_REPLAY_READ_SCHEMA,
        "run_id": entry["run_id"],
        "workflow_run_id": manifest["workflow_run_id"],
        "job_id": manifest["job_id"],
        "manifest": manifest,
        "events": events,
        "artifact_index": package["artifact_index"],
        "typed_artifacts": typed_artifacts,
        "artifacts": artifact_bodies,
        "validated_artifacts": validated_artifacts,
        "artifact_links": {
            row["binding"]["artifact_ref"]: (
                f"/api/thesis/runs/{entry['run_id']}/workflow-replay/artifact"
                "?artifact_ref="
                + quote(row["binding"]["artifact_ref"], safe="")
            )
            for row in package["artifact_index"]["artifacts"]
        },
        "usage": _usage_read_model(
            events=package["events"],
            typed_artifacts=typed_artifacts,
            workflow_run_id=manifest["workflow_run_id"],
        ),
        "evaluation_scope": evaluation_scope,
        "evaluation_console": evaluation_console,
        "source_mode": source_mode,
        "cursor": {
            "after_seq": after_seq,
            "returned_through_seq": returned_through,
            "through_seq": latest_seq,
            "latest_seq": latest_seq,
            "terminal": manifest["status"] in {"failed", "succeeded"},
            "latest_event_sha256": (
                package["events"][-1]["integrity"]["event_sha256"]
                if package["events"]
                else None
            ),
            "event_chain_head_sha256": (
                package["events"][-1]["integrity"]["event_sha256"]
                if package["events"]
                else None
            ),
            "manifest_sha256": manifest["integrity"]["manifest_sha256"],
            "package_revision_sha256": manifest["integrity"][
                "manifest_sha256"
            ],
            "artifact_index_sha256": package["artifact_index"]["integrity"][
                "artifact_index_sha256"
            ],
        },
        "actions": _actions(
            entry,
            manifest,
            jobs_root=jobs_root,
            evaluation_scope=evaluation_scope,
            source_mode=source_mode,
        ),
    }


def _typed_artifacts(
    root: Path,
    artifact_index: Mapping[str, Any],
) -> list[dict[str, Any]]:
    selected_kinds = {
        "scoring_handoff",
        "scoring_handoff_v1",
        "scoring_receipt",
        "scoring_receipt_v1",
        "full_run_report",
        "full_run_report_v1",
        "benchmark_run_report_v1",
        "evaluation_report_v1",
        "workflow_usage_summary",
        "workflow_usage_summary_v1",
        "component_usage_snapshot",
        "d2l_component_usage_snapshot_v1",
        "evaluation_component_usage_snapshot_v1",
        "evaluation_workflow_settings_v1",
        "evaluation_console_projection_v1",
    }
    result: list[dict[str, Any]] = []
    for artifact in artifact_index["artifacts"]:
        binding = artifact["binding"]
        ref = binding["artifact_ref"]
        kind = binding["artifact_kind"]
        if kind not in selected_kinds and not any(
            marker in ref
            for marker in ("scoring_handoff", "scoring_receipt", "usage_summary")
        ):
            continue
        path = _resolve_artifact(root, ref)
        if path.suffix.lower() != ".json":
            continue
        raw = _read_regular_bytes(path, max_bytes=MAX_ARTIFACT_BYTES)
        if physical_sha256(raw) != artifact["imported_physical_sha256"]:
            raise WorkflowReplayError(
                "workflow_artifact_hash_drift",
                "Typed workflow artifact bytes no longer match their receipt.",
                409,
            )
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowReplayError(
                "workflow_artifact_json_invalid",
                "Typed workflow artifact is not valid UTF-8 JSON.",
                409,
            ) from exc
        result.append({"binding": binding, "body": body})
    return result


def _evaluation_console_read_model(
    *,
    manifest: Mapping[str, Any],
    events: list[dict[str, Any]],
    artifact_index: Mapping[str, Any],
    typed_artifacts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    from pipeline.eval.contracts_v1 import ContractValidationError
    from pipeline.eval.workflow_console_projection_v1 import (
        CONSOLE_PROJECTION_ARTIFACT_KIND,
        CONSOLE_PROJECTION_SCHEMA_VERSION,
        console_projection_artifact_ref_v1,
        validate_evaluation_console_projection_v1,
    )

    indexed = [
        row
        for row in artifact_index["artifacts"]
        if row["binding"]["artifact_kind"] == CONSOLE_PROJECTION_ARTIFACT_KIND
    ]
    if not indexed:
        return None

    typed_by_ref = {
        row["binding"]["artifact_ref"]: row
        for row in typed_artifacts
        if row["binding"]["artifact_kind"] == CONSOLE_PROJECTION_ARTIFACT_KIND
    }
    if len(typed_by_ref) != len(indexed):
        raise WorkflowReplayError(
            "workflow_evaluation_console_exact_cover",
            "Evaluation Console projection artifacts are not exact-covered by the parent read model.",
            409,
        )

    component_rows = [
        row
        for row in manifest["components"]
        if row["component_id"] == "evaluation"
    ]
    evaluation_events = [
        event
        for event in events
        if event["component"]["component_id"] == "evaluation"
    ]
    if len(component_rows) != 1 or not evaluation_events:
        raise WorkflowReplayError(
            "workflow_evaluation_console_component_binding",
            "Evaluation Console projections require one accepted Evaluation component event stream.",
            409,
        )
    component = component_rows[0]
    if len(indexed) != len(evaluation_events):
        raise WorkflowReplayError(
            "workflow_evaluation_console_exact_cover",
            "Evaluation Console projections must exact-cover the accepted Evaluation component events.",
            409,
        )

    event_by_component_seq = {
        event["component"]["component_seq"]: event
        for event in evaluation_events
    }
    if len(event_by_component_seq) != len(evaluation_events):
        raise WorkflowReplayError(
            "workflow_evaluation_console_sequence",
            "Evaluation component sequence is not unique.",
            409,
        )

    accepted: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    for artifact in indexed:
        ref = artifact["binding"]["artifact_ref"]
        typed = typed_by_ref.get(ref)
        if typed is None:
            raise WorkflowReplayError(
                "workflow_evaluation_console_exact_cover",
                "Evaluation Console projection body is missing.",
                409,
            )
        try:
            projection = validate_evaluation_console_projection_v1(
                typed["body"]
            )
        except ContractValidationError as exc:
            raise WorkflowReplayError(
                "workflow_evaluation_console_invalid",
                f"Evaluation Console projection failed validation: {exc}",
                409,
            ) from exc
        accepted.append((projection, artifact))
    accepted.sort(key=lambda pair: pair[0]["projection_index"])

    previous_projection_sha256 = None
    previous_parent_ref = None
    previous_prefixes: Mapping[str, Any] | None = None
    row_count = 0
    row_chain_sha256 = "0" * 64
    seen_row_ids: set[str] = set()
    cumulative_rows: list[dict[str, Any]] = []
    for expected_index, (projection, artifact) in enumerate(
        accepted,
        start=1,
    ):
        if projection["projection_index"] != expected_index:
            raise WorkflowReplayError(
                "workflow_evaluation_console_sequence",
                "Evaluation Console projection sequence is not contiguous.",
                409,
            )
        event = event_by_component_seq.get(expected_index)
        if event is None:
            raise WorkflowReplayError(
                "workflow_evaluation_console_exact_cover",
                "Evaluation Console projection references a missing parent event.",
                409,
            )
        source = event["component"]
        binding = artifact["binding"]
        expected_component_ref = console_projection_artifact_ref_v1(
            projection
        )
        expected_parent_ref = (
            f"components/evaluation/{projection['component_run_id']}/artifacts/"
            f"{expected_component_ref}"
        )
        expected_parent_refs = (
            [] if previous_parent_ref is None else [previous_parent_ref]
        )
        if (
            projection["workflow_run_id"] != manifest["workflow_run_id"]
            or projection["component_run_id"] != component["component_run_id"]
            or projection["component_run_id"] != source["component_run_id"]
            or projection["through_component_seq"] != source["component_seq"]
            or projection["through_component_event_id"]
            != source["source_event_id"]
            or projection["component_attempt_id"]
            != source["component_attempt_id"]
            or projection["component_attempt_index"]
            != source["component_attempt_index"]
            or artifact["component_artifact_ref"] != expected_component_ref
            or binding["artifact_ref"] != expected_parent_ref
            or binding["schema_version"]
            != CONSOLE_PROJECTION_SCHEMA_VERSION
            or binding["sha256_kind"] != "physical"
            or binding["sha256"] != artifact["imported_physical_sha256"]
            or artifact["created_event_id"] != event["event_id"]
            or artifact["parent_artifact_refs"] != expected_parent_refs
            or artifact["producer"]["component_id"] != "evaluation"
            or artifact["producer"]["component_run_id"]
            != source["component_run_id"]
            or artifact["producer"]["component_attempt_id"]
            != source["component_attempt_id"]
            or artifact["producer"]["component_attempt_index"]
            != source["component_attempt_index"]
            or projection["previous_projection_sha256"]
            != previous_projection_sha256
        ):
            raise WorkflowReplayError(
                "workflow_evaluation_console_binding_drift",
                "Evaluation Console projection differs from its validated parent binding.",
                409,
            )

        prefixes = projection["prefixes"]
        if previous_prefixes is not None:
            for prefix_name in ("recovery_journal", "diagnostics"):
                previous_prefix = previous_prefixes[prefix_name]
                current_prefix = prefixes[prefix_name]
                if (
                    current_prefix["record_count"]
                    < previous_prefix["record_count"]
                    or (
                        current_prefix["record_count"]
                        == previous_prefix["record_count"]
                        and current_prefix["prefix_sha256"]
                        != previous_prefix["prefix_sha256"]
                    )
                ):
                    raise WorkflowReplayError(
                        "workflow_evaluation_console_prefix_drift",
                        "Evaluation Console recovery or diagnostic prefix is not monotonic.",
                        409,
                    )
        for row in projection["rows"]:
            if row["row_id"] in seen_row_ids:
                raise WorkflowReplayError(
                    "workflow_evaluation_console_row_duplicate",
                    "Evaluation Console projection repeats a producer row.",
                    409,
                )
            if row["source_component_seq_end"] > expected_index:
                raise WorkflowReplayError(
                    "workflow_evaluation_console_future_prefix",
                    "Evaluation Console row references a future component event.",
                    409,
                )
            seen_row_ids.add(row["row_id"])
            row_chain_sha256 = canonical_sha256(
                {
                    "previous_row_chain_sha256": row_chain_sha256,
                    "row_sha256": row["integrity"]["row_sha256"],
                }
            )
            row_count += 1
            cumulative_rows.append(row)
        if (
            projection["cumulative"]["row_count"] != row_count
            or projection["cumulative"]["row_chain_sha256"]
            != row_chain_sha256
        ):
            raise WorkflowReplayError(
                "workflow_evaluation_console_row_chain_drift",
                "Evaluation Console cumulative row chain has drifted.",
                409,
            )

        previous_projection_sha256 = projection["integrity"][
            "projection_sha256"
        ]
        previous_parent_ref = binding["artifact_ref"]
        previous_prefixes = prefixes

    latest_projection, latest_artifact = accepted[-1]
    latest_event = event_by_component_seq[
        latest_projection["through_component_seq"]
    ]
    if (
        component["last_component_seq"]
        != latest_projection["through_component_seq"]
    ):
        raise WorkflowReplayError(
            "workflow_evaluation_console_component_cursor_drift",
            "Evaluation Console cursor differs from the parent component cursor.",
            409,
        )
    read_model = {
        "schema_id": "EvaluationConsoleReadV1",
        "schema_version": "1.0.0",
        "workflow_run_id": manifest["workflow_run_id"],
        "component_id": "evaluation",
        "component_run_id": latest_projection["component_run_id"],
        "projection_count": len(accepted),
        "through_parent_seq": latest_event["seq"],
        "through_parent_event_id": latest_event["event_id"],
        "through_component_seq": latest_projection[
            "through_component_seq"
        ],
        "through_component_event_id": latest_projection[
            "through_component_event_id"
        ],
        "projection_ref": latest_artifact["binding"]["artifact_ref"],
        "projection_sha256": latest_projection["integrity"][
            "projection_sha256"
        ],
        "rows": cumulative_rows,
        "state": latest_projection["state"],
        "cumulative": latest_projection["cumulative"],
        "validation": {
            "valid": True,
            "authority": (
                "evaluation_console_projection_v1_and_neutral_relay"
            ),
            "artifact_index_sha256": artifact_index["integrity"][
                "artifact_index_sha256"
            ],
            "parent_event_sha256": latest_event["integrity"][
                "event_sha256"
            ],
        },
    }
    return {
        **read_model,
        "integrity": {
            "read_sha256": canonical_sha256(read_model),
        },
    }


def _evaluation_scope_read_model(
    entry: Mapping[str, Any],
    *,
    typed_artifacts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    selection = entry.get("evaluation_selection")
    if selection is None:
        return None
    if not isinstance(selection, Mapping):
        raise WorkflowReplayError(
            "workflow_evaluation_selection_invalid",
            "Run registry Evaluation selection is not an object.",
            409,
        )
    required = {
        "settings_option_id",
        "selected_chapter_ids",
        "selected_arm_ids",
        "selected_scorer_ids",
        "highlight_pair",
        "registered_option_sha256",
        "selection_sha256",
    }
    if set(selection) != required:
        raise WorkflowReplayError(
            "workflow_evaluation_selection_invalid",
            "Run registry Evaluation selection has contract drift.",
            409,
        )
    basis = {key: selection[key] for key in required - {"selection_sha256"}}
    selection_sha256 = canonical_sha256(basis)
    if (
        selection["selection_sha256"] != selection_sha256
        or entry.get("evaluation_selection_sha256") != selection_sha256
        or entry.get("evaluation_settings_template_sha256")
        != selection["registered_option_sha256"]
    ):
        raise WorkflowReplayError(
            "workflow_evaluation_selection_hash_drift",
            "Run registry Evaluation selection hash binding has drifted.",
            409,
        )

    bodies = [
        row["body"]
        for row in typed_artifacts
        if isinstance(row.get("body"), Mapping)
    ]
    handoffs = [
        body
        for body in bodies
        if body.get("schema_id") == "ScoringHandoffV1"
        or body.get("schema") == "scoring_handoff_v1"
    ]
    settings_by_sha256 = {
        canonical_sha256(body): body
        for body in bodies
        if body.get("schema_id") == EVALUATION_SETTINGS_SCHEMA_ID
    }
    settings_rows = list(settings_by_sha256.values())
    if len(handoffs) > 1 or len(settings_rows) > 1:
        raise WorkflowReplayError(
            "workflow_evaluation_settings_ambiguous",
            "Parent package contains duplicate Evaluation authority artifacts.",
            409,
        )
    settings_sha256 = None
    settings_status = "pending_scoring_handoff"
    if settings_rows:
        if not handoffs:
            raise WorkflowReplayError(
                "workflow_evaluation_settings_without_handoff",
                "Evaluation settings appeared before the scoring handoff.",
                409,
            )
        from pipeline.eval.evaluation_workflow_settings_v1 import (
            validate_evaluation_workflow_settings_v1,
        )

        try:
            settings = validate_evaluation_workflow_settings_v1(
                settings_rows[0],
                scoring_handoff=handoffs[0],
            )
        except Exception as exc:
            raise WorkflowReplayError(
                "workflow_evaluation_settings_invalid",
                f"Evaluation settings failed validation: {exc}",
                409,
            ) from exc
        expected = {
            "selected_chapter_ids": selection["selected_chapter_ids"],
            "selected_arm_ids": selection["selected_arm_ids"],
            "selected_scorer_ids": selection["selected_scorer_ids"],
            "highlight_pair": selection["highlight_pair"],
        }
        observed = {key: settings[key] for key in expected}
        if observed != expected:
            raise WorkflowReplayError(
                "workflow_evaluation_settings_selection_drift",
                "Materialized Evaluation settings differ from the sealed selection.",
                409,
            )
        settings_sha256 = settings["settings_sha256"]
        settings_status = "materialized"
    elif handoffs:
        settings_status = "pending_settings_materialization"

    return {
        "schema_id": "EvaluationWorkflowScopeReadV1",
        "schema_version": "1.0.0",
        "settings_option_id": selection["settings_option_id"],
        "registered_option_sha256": selection[
            "registered_option_sha256"
        ],
        "selection_sha256": selection_sha256,
        "selected_chapter_ids": list(selection["selected_chapter_ids"]),
        "selected_arm_ids": list(selection["selected_arm_ids"]),
        "selected_scorer_ids": list(selection["selected_scorer_ids"]),
        "highlight_pair": selection["highlight_pair"],
        "scoring_handoff_status": (
            "validated" if handoffs else "pending"
        ),
        "settings_status": settings_status,
        "settings_sha256": settings_sha256,
    }


def _validated_artifacts(
    typed_artifacts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    from pipeline.eval.full_run_report_v1 import validate_full_run_report

    report_kinds = {
        "full_run_report_v1",
        "benchmark_run_report_v1",
        "evaluation_report_v1",
    }
    result: dict[str, dict[str, Any]] = {}
    for item in typed_artifacts:
        binding = item["binding"]
        if binding["artifact_kind"] not in report_kinds:
            continue
        try:
            validate_full_run_report(item["body"])
        except Exception as exc:
            raise WorkflowReplayError(
                "workflow_report_invalid",
                f"Indexed Evaluation report failed validation: {exc}",
                409,
            ) from exc
        result[binding["artifact_ref"]] = {
            "valid": True,
            "sha256": binding["sha256"],
            "sha256_kind": binding["sha256_kind"],
        }
    return result


def _usage_read_model(
    *,
    events: list[dict[str, Any]],
    typed_artifacts: list[dict[str, Any]],
    workflow_run_id: str,
) -> dict[str, Any] | None:
    calls: list[dict[str, Any]] = []
    stage_totals: list[dict[str, Any]] = []
    component_totals: list[dict[str, Any]] = []

    d2l_events = [
        event
        for event in events
        if event["component"]["component_id"] == "translation"
        and event["event"] in {"transport_attempt_failed", "usage_snapshot"}
    ]
    if d2l_events:
        (
            d2l_calls,
            d2l_stage_totals,
            d2l_component_total,
        ) = _project_d2l_usage(d2l_events, workflow_run_id=workflow_run_id)
        calls.extend(d2l_calls)
        stage_totals.extend(d2l_stage_totals)
        component_totals.append(d2l_component_total)

    evaluation_events = [
        event
        for event in events
        if event["component"]["component_id"] == "evaluation"
        and event["event"] == "usage_snapshot"
    ]
    evaluation_artifacts = [
        item
        for item in typed_artifacts
        if item["binding"]["artifact_kind"]
        == "evaluation_component_usage_snapshot_v1"
    ]
    if evaluation_events or evaluation_artifacts:
        (
            evaluation_calls,
            evaluation_stage_totals,
            evaluation_component_total,
        ) = _project_evaluation_usage(
            evaluation_events,
            evaluation_artifacts,
            workflow_run_id=workflow_run_id,
        )
        calls.extend(evaluation_calls)
        stage_totals.extend(evaluation_stage_totals)
        component_totals.append(evaluation_component_total)

    workflow_total = _indexed_workflow_usage_total(
        typed_artifacts,
        workflow_run_id=workflow_run_id,
    )
    if not calls and not component_totals and workflow_total is None:
        return None
    return {
        "schema_id": "WorkflowUsageReadModelV1",
        "schema_version": "1.0.0",
        "workflow_run_id": workflow_run_id,
        "validated": True,
        "calls": calls,
        "stage_totals": stage_totals,
        "component_totals": component_totals,
        "workflow_total": workflow_total,
        "validation": {
            "valid": True,
            "authority": "producer_snapshots_and_neutral_relay",
        },
    }


def _project_evaluation_usage(
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    *,
    workflow_run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    from pipeline.eval.contracts_v1 import ContractValidationError
    from pipeline.eval.evaluation_component_usage_v1 import (
        validate_evaluation_component_usage_snapshot_chain_v1,
    )

    if not events or not artifacts or len(events) != len(artifacts):
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            "Evaluation usage events and indexed snapshots do not exact-cover each other.",
            409,
        )
    ordered_artifacts = sorted(
        artifacts,
        key=lambda item: item["body"].get("snapshot_index", 0),
    )
    snapshots = [item["body"] for item in ordered_artifacts]
    first = snapshots[0]
    if not isinstance(first, Mapping):
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            "Evaluation usage snapshot is not an object.",
            409,
        )
    component_run_id = first.get("component_run_id")
    stage_rows = first.get("stage_totals")
    if not isinstance(component_run_id, str) or not isinstance(stage_rows, list):
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            "Evaluation usage snapshot identity or stage catalog is malformed.",
            409,
        )
    stage_ids = tuple(
        row.get("stage_id")
        for row in stage_rows
        if isinstance(row, Mapping) and isinstance(row.get("stage_id"), str)
    )
    if len(stage_ids) != len(stage_rows):
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            "Evaluation usage snapshot has a malformed stage catalog.",
            409,
        )
    try:
        normalized = list(
            validate_evaluation_component_usage_snapshot_chain_v1(
                snapshots,
                workflow_run_id=workflow_run_id,
                component_run_id=component_run_id,
                stage_ids=stage_ids,
            )
        )
    except ContractValidationError as exc:
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            f"Evaluation usage snapshot chain failed validation: {exc}",
            409,
        ) from exc

    expected_hashes = []
    for item, snapshot in zip(ordered_artifacts, normalized, strict=True):
        binding = item["binding"]
        snapshot_hash = snapshot["integrity"]["usage_snapshot_sha256"]
        if (
            binding["schema_version"] != snapshot["schema_version"]
            or binding["sha256"] != snapshot_hash
            or binding["sha256_kind"]
            != "canonical:EvaluationComponentUsageSnapshotV1@1.0.0"
        ):
            raise WorkflowReplayError(
                "workflow_usage_binding_drift",
                "Evaluation usage snapshot differs from its indexed binding.",
                409,
            )
        expected_hashes.append(snapshot_hash)

    calls = []
    cache_counts: dict[str, dict[str, int]] = {
        stage_id: {"hit": 0, "miss": 0} for stage_id in stage_ids
    }
    for event, snapshot, expected_hash in zip(
        events, normalized, expected_hashes, strict=True
    ):
        payload = event["payload"]
        snapshot_binding = (
            payload.get("snapshot") if isinstance(payload, Mapping) else None
        )
        if (
            not isinstance(snapshot_binding, Mapping)
            or snapshot_binding.get("artifact_kind")
            != "evaluation_component_usage_snapshot_v1"
            or snapshot_binding.get("schema_version")
            != snapshot["schema_version"]
            or snapshot_binding.get("sha256") != expected_hash
            or snapshot_binding.get("sha256_kind")
            != "canonical:EvaluationComponentUsageSnapshotV1@1.0.0"
            or event["component"]["component_run_id"]
            != snapshot["component_run_id"]
            or event["component"]["component_attempt_id"]
            != snapshot["component_attempt_id"]
            or event["component"]["component_attempt_index"]
            != snapshot["component_attempt_index"]
            or event["component"]["component_seq"]
            != snapshot["accepted_through_component_seq"]
        ):
            raise WorkflowReplayError(
                "workflow_usage_binding_drift",
                "Evaluation usage event differs from its producer-sealed snapshot.",
                409,
            )

        current = snapshot["current_record"]
        if current["kind"] == "final":
            continue
        evidence = current["evidence"]
        stage_id = f"evaluation.{evidence['stage_id']}"
        if current["kind"] == "usage":
            usage = evidence["attempt_usage"]
            calls.append(
                {
                    "call_id": f"evaluation:{usage['attempt_usage_id']}",
                    "attempt_usage_id": usage["attempt_usage_id"],
                    "cache_observation_id": None,
                    "component_id": "evaluation",
                    "component_run_id": snapshot["component_run_id"],
                    "component_attempt_id": snapshot["component_attempt_id"],
                    "component_attempt_index": snapshot[
                        "component_attempt_index"
                    ],
                    "component_seq": event["component"]["component_seq"],
                    "stage_id": stage_id,
                    "agent": evidence["role_id"],
                    "work_id": snapshot["current_work_id"],
                    "logical_request_id": usage["logical_request_id"],
                    "semantic_attempt_index": usage["semantic_attempt_index"],
                    "transport_retry_ordinal": usage[
                        "transport_retry_ordinal"
                    ],
                    "physical_attempt_index": usage["physical_attempt_index"],
                    "provider_id": None,
                    "source_id": usage["source_id"],
                    "source_revision": usage["source_revision"],
                    "requested_model_id": usage["requested_model_id"],
                    "observed_model_id": usage["observed_model_id"],
                    "provider_call_avoided": False,
                    "finish_reason": usage["finish_reason"],
                    "outcome": usage["outcome"],
                    "usage": {
                        "prompt_tokens": usage["prompt_tokens"],
                        "cached_input_tokens": usage["cached_input_tokens"],
                        "completion_tokens": usage["completion_tokens"],
                        "reasoning_tokens": usage["reasoning_tokens"],
                        "total_tokens": usage["total_tokens"],
                        "latency_ms": usage["latency_ms"],
                        "cost_usd": usage["cost_usd"],
                        "cost_status": usage["cost_status"],
                        "currency": "USD",
                    },
                }
            )
            continue

        observation = evidence["cache_observation"]
        cache_status = {
            "bypassed": "bypass",
            "not_checked": "unknown",
        }.get(observation["lookup_status"], observation["lookup_status"])
        cache_mechanism = {
            "application_response_cache": "local_exact_cache",
            "checkpoint_stage_reuse": "local_exact_cache",
            "retrieval_context_cache": "local_exact_cache",
        }.get(observation["cache_kind"], observation["cache_kind"])
        if cache_status in cache_counts[evidence["stage_id"]]:
            cache_counts[evidence["stage_id"]][cache_status] += 1
        calls.append(
            {
                "call_id": f"evaluation:{observation['observation_id']}",
                "attempt_usage_id": None,
                "cache_observation_id": observation["observation_id"],
                "component_id": "evaluation",
                "component_run_id": snapshot["component_run_id"],
                "component_attempt_id": snapshot["component_attempt_id"],
                "component_attempt_index": snapshot["component_attempt_index"],
                "component_seq": event["component"]["component_seq"],
                "stage_id": stage_id,
                "agent": evidence["role_id"],
                "work_id": snapshot["current_work_id"],
                "logical_request_id": observation["logical_request_id"],
                "semantic_attempt_index": None,
                "transport_retry_ordinal": None,
                "physical_attempt_index": None,
                "provider_id": None,
                "source_id": evidence["execution_target"]["source_id"],
                "source_revision": evidence["execution_target"][
                    "source_revision"
                ],
                "requested_model_id": evidence["execution_target"][
                    "requested_model_id"
                ],
                "observed_model_id": evidence["execution_target"][
                    "observed_model_id"
                ],
                "provider_call_avoided": observation[
                    "provider_call_avoided"
                ],
                "finish_reason": None,
                "outcome": None,
                "usage": {
                    "prompt_tokens": None,
                    "cached_input_tokens": None,
                    "completion_tokens": None,
                    "reasoning_tokens": None,
                    "total_tokens": None,
                    "latency_ms": None,
                    "cache_status": cache_status,
                    "cache_mechanism": cache_mechanism,
                    "cost_usd": 0.0,
                    "cost_status": "not_applicable",
                    "currency": "USD",
                },
            }
        )

    latest = normalized[-1]
    stage_totals = [
        _project_evaluation_total(
            row,
            component_run_id=latest["component_run_id"],
            component_attempt_id=latest["component_attempt_id"],
            component_attempt_index=latest["component_attempt_index"],
            snapshot_index=latest["snapshot_index"],
            accepted_through_component_seq=latest[
                "accepted_through_component_seq"
            ],
            stage_id=f"evaluation.{row['stage_id']}",
            snapshot_sha256=latest["integrity"]["usage_snapshot_sha256"],
            cache_counts=cache_counts[row["stage_id"]],
        )
        for row in latest["stage_totals"]
    ]
    component_cache_counts = {
        key: sum(stage[key] for stage in cache_counts.values())
        for key in ("hit", "miss")
    }
    component_total = _project_evaluation_total(
        latest["component_totals"],
        component_run_id=latest["component_run_id"],
        component_attempt_id=latest["component_attempt_id"],
        component_attempt_index=latest["component_attempt_index"],
        snapshot_index=latest["snapshot_index"],
        accepted_through_component_seq=latest[
            "accepted_through_component_seq"
        ],
        stage_id=None,
        snapshot_sha256=latest["integrity"]["usage_snapshot_sha256"],
        cache_counts=component_cache_counts,
    )
    return calls, stage_totals, component_total


def _project_evaluation_total(
    totals: Mapping[str, Any],
    *,
    component_run_id: str,
    component_attempt_id: str,
    component_attempt_index: int,
    snapshot_index: int,
    accepted_through_component_seq: int,
    stage_id: str | None,
    snapshot_sha256: str,
    cache_counts: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "component_id": "evaluation",
        "component_run_id": component_run_id,
        "component_attempt_id": component_attempt_id,
        "component_attempt_index": component_attempt_index,
        "stage_id": stage_id,
        "snapshot_seq": snapshot_index,
        "accepted_through_component_seq": accepted_through_component_seq,
        "physical_call_count": totals["physical_attempt_count"],
        "cache_observation_count": totals["cache_observation_count"],
        "prompt_tokens": totals["prompt_tokens"],
        "completion_tokens": totals["completion_tokens"],
        "reasoning_tokens": totals["reasoning_tokens"],
        "cached_input_tokens": totals["cached_input_tokens"],
        "total_tokens": totals["total_tokens"],
        "cache_hit_count": cache_counts["hit"],
        "cache_miss_count": cache_counts["miss"],
        "unknown_attempt_count": totals["unknown_attempt_count"],
        "cost_status": totals["cost_status"],
        "cost_usd": totals["cost_usd"],
        "currency": "USD",
        "snapshot_sha256": snapshot_sha256,
    }


def _project_d2l_usage(
    events: list[dict[str, Any]],
    *,
    workflow_run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    from pipeline.prepass.d2l_console_replay_contract_v1 import (
        D2LConsoleContractError,
        validate_component_usage_snapshot_sequence,
    )

    snapshot_events = [
        event for event in events if event["event"] == "usage_snapshot"
    ]
    snapshots = [event["payload"] for event in snapshot_events]
    latest = None
    if snapshots:
        try:
            latest = validate_component_usage_snapshot_sequence(snapshots)
        except D2LConsoleContractError as exc:
            raise WorkflowReplayError(
                "workflow_usage_invalid",
                f"D2L usage snapshot chain failed validation: {exc}",
                409,
            ) from exc
        if latest is None:
            raise WorkflowReplayError(
                "workflow_usage_invalid",
                "D2L usage events did not produce a cumulative snapshot.",
                409,
            )
        if latest["workflow_run_id"] != workflow_run_id:
            raise WorkflowReplayError(
                "workflow_usage_identity_drift",
                "D2L usage snapshots belong to another parent workflow.",
                409,
            )

    calls: list[dict[str, Any]] = []
    latest_stage: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    failure_calls_by_stage: dict[str, list[dict[str, Any]]] = {}
    stage_order: list[str] = []
    for event in events:
        if event["event"] == "transport_attempt_failed":
            call = _project_d2l_transport_failure(
                event,
                workflow_run_id=workflow_run_id,
            )
            stage_id = call["stage_id"]
            if stage_id not in stage_order:
                stage_order.append(stage_id)
            failure_calls_by_stage.setdefault(stage_id, []).append(call)
            calls.append(call)
            continue

        snapshot = event["payload"]
        accepted = snapshot["accepted_usage"]
        if accepted is None:
            continue
        stage_id = f"translation.{snapshot['stage_id']}"
        if stage_id not in stage_order:
            stage_order.append(stage_id)
        latest_stage[stage_id] = (snapshot, event)
        usage = accepted["usage"]
        calls.append(
            {
                "call_id": (
                    accepted["attempt_usage_id"]
                    or accepted["cache_observation_id"]
                ),
                "attempt_usage_id": accepted["attempt_usage_id"],
                "cache_observation_id": accepted["cache_observation_id"],
                "component_id": "translation",
                "component_run_id": snapshot["component_run_id"],
                "component_attempt_id": snapshot["component_attempt_id"],
                "component_attempt_index": snapshot["component_attempt_id"],
                "component_seq": event["component"]["component_seq"],
                "stage_id": stage_id,
                "work_id": snapshot["work_id"],
                "logical_request_id": accepted["logical_request_id"],
                "semantic_attempt_index": accepted["semantic_attempt_index"],
                "transport_retry_ordinal": accepted[
                    "transport_retry_ordinal"
                ],
                "physical_attempt_index": accepted[
                    "physical_attempt_index"
                ],
                "provider_id": usage["provider_id"],
                "source_id": usage["source_id"],
                "source_revision": accepted["source_revision"],
                "requested_model_id": usage["model_id"],
                "observed_model_id": None,
                "provider_call_avoided": not accepted["provider_called"],
                "finish_reason": usage["finish_reason"],
                "usage": dict(usage),
            }
        )

    call_ids = [call["call_id"] for call in calls]
    if any(not isinstance(call_id, str) or not call_id for call_id in call_ids):
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            "D2L usage evidence has no stable call identity.",
            409,
        )
    if len(call_ids) != len(set(call_ids)):
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            "D2L usage evidence repeats a physical/cache identity.",
            409,
        )

    projected_stage_totals = []
    for stage_id in stage_order:
        snapshot_item = latest_stage.get(stage_id)
        failures = failure_calls_by_stage.get(stage_id, [])
        if snapshot_item is not None:
            snapshot, snapshot_event = snapshot_item
            totals = snapshot["stage_cumulative"]
            component_run_id = snapshot["component_run_id"]
            component_attempt_id = snapshot["component_attempt_id"]
            snapshot_seq: int | None = snapshot["snapshot_seq"]
            accepted_through_component_seq = snapshot_event["component"][
                "component_seq"
            ]
            sha256: str | None = snapshot["snapshot_sha256"]
        else:
            totals = _empty_d2l_usage_totals()
            latest_failure = max(
                failures,
                key=lambda row: row["component_seq"],
            )
            component_run_id = latest_failure["component_run_id"]
            component_attempt_id = latest_failure["component_attempt_id"]
            snapshot_seq = None
            accepted_through_component_seq = latest_failure["component_seq"]
            sha256 = None
        if failures:
            latest_failure = max(
                failures,
                key=lambda row: row["component_seq"],
            )
            if (
                latest_failure["component_seq"]
                > accepted_through_component_seq
            ):
                component_run_id = latest_failure["component_run_id"]
                component_attempt_id = latest_failure[
                    "component_attempt_id"
                ]
            accepted_through_component_seq = max(
                accepted_through_component_seq,
                *(row["component_seq"] for row in failures),
            )
        projected_stage_totals.append(
            _project_d2l_total(
                totals,
                component_run_id=component_run_id,
                component_attempt_id=component_attempt_id,
                snapshot_seq=snapshot_seq,
                accepted_through_component_seq=(
                    accepted_through_component_seq
                ),
                stage_id=stage_id,
                sha256=sha256,
                failures=failures,
            )
        )

    last_event = events[-1]
    component_totals = (
        latest["component_cumulative"]
        if latest is not None
        else _empty_d2l_usage_totals()
    )
    component_snapshot_seq = (
        latest["snapshot_seq"] if latest is not None else None
    )
    component_snapshot_sha256 = (
        latest["snapshot_sha256"] if latest is not None else None
    )
    failure_calls = [
        call for call in calls if call.get("outcome") == "transport_failed"
    ]
    component_total = _project_d2l_total(
        component_totals,
        component_run_id=last_event["component"]["component_run_id"],
        component_attempt_id=last_event["component"][
            "component_attempt_id"
        ],
        snapshot_seq=component_snapshot_seq,
        accepted_through_component_seq=max(
            event["component"]["component_seq"] for event in events
        ),
        stage_id=None,
        sha256=component_snapshot_sha256,
        failures=failure_calls,
    )
    return calls, projected_stage_totals, component_total


_D2L_TRANSPORT_FAILURE_KEYS = {
    "attempt_usage_id",
    "logical_request_id",
    "semantic_attempt_index",
    "transport_retry_ordinal",
    "physical_attempt_index",
    "work_kind",
    "work_id",
    "provider_id",
    "model_id",
    "source_id",
    "source_revision",
    "masked_quota_bucket",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cost_usd",
    "cost_status",
    "reason_code",
    "retry_class",
    "retry_disposition",
}
_D2L_TRANSPORT_FAILURE_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _project_d2l_transport_failure(
    event: Mapping[str, Any],
    *,
    workflow_run_id: str,
) -> dict[str, Any]:
    def reject(message: str) -> None:
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            f"D2L transport failure is invalid: {message}",
            409,
        )

    if event.get("workflow_run_id") != workflow_run_id:
        raise WorkflowReplayError(
            "workflow_usage_identity_drift",
            "D2L transport failure belongs to another parent workflow.",
            409,
        )
    component = event.get("component")
    if not isinstance(component, Mapping):
        reject("component identity is missing")
    if component.get("component_id") != "translation":
        reject("component identity is not translation")
    for key in ("component_run_id",):
        if not isinstance(component.get(key), str) or not component[key]:
            reject(f"{key} is invalid")
    for key in (
        "component_attempt_id",
        "component_attempt_index",
        "component_seq",
    ):
        value = component.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            reject(f"{key} is invalid")
    if component["component_attempt_id"] != component[
        "component_attempt_index"
    ]:
        reject("component attempt identity is inconsistent")

    stage_id = event.get("stage_id")
    if not isinstance(stage_id, str) or not stage_id.startswith(
        "translation."
    ):
        reject("stage identity is invalid")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        reject("payload is not an object")
    if set(payload) != _D2L_TRANSPORT_FAILURE_KEYS:
        reject("payload keys do not match the sealed producer contract")
    for key in (
        "attempt_usage_id",
        "logical_request_id",
        "work_kind",
        "work_id",
        "provider_id",
        "model_id",
        "source_id",
        "source_revision",
        "masked_quota_bucket",
        "reason_code",
        "retry_class",
        "retry_disposition",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            reject(f"{key} is invalid")
    for key, minimum in (
        ("semantic_attempt_index", 1),
        ("transport_retry_ordinal", 0),
        ("physical_attempt_index", 1),
        ("latency_ms", 0),
    ):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            reject(f"{key} is invalid")

    token_values = [payload[key] for key in _D2L_TRANSPORT_FAILURE_TOKEN_KEYS]
    if any(value is None for value in token_values):
        if any(value is not None for value in token_values):
            reject("token facts must be all null or all known")
    else:
        for key in _D2L_TRANSPORT_FAILURE_TOKEN_KEYS:
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                reject(f"{key} is invalid")
        if payload["total_tokens"] != (
            payload["prompt_tokens"] + payload["completion_tokens"]
        ):
            reject("total_tokens is inconsistent")
    if payload["cost_status"] != "unknown" or payload["cost_usd"] is not None:
        reject("unavailable failure cost must remain unknown/null")
    if payload["retry_disposition"] not in {
        "transport_retry_allowed",
        "do_not_retry",
    }:
        reject("retry disposition is invalid")

    usage = {
        "logical_request_id": payload["logical_request_id"],
        "physical_attempt_index": payload["physical_attempt_index"],
        "provider_id": payload["provider_id"],
        "model_id": payload["model_id"],
        "source_id": payload["source_id"],
        "masked_quota_bucket": payload["masked_quota_bucket"],
        "prompt_tokens": payload["prompt_tokens"],
        "completion_tokens": payload["completion_tokens"],
        "cached_input_tokens": payload["cached_input_tokens"],
        "reasoning_tokens": payload["reasoning_tokens"],
        "total_tokens": payload["total_tokens"],
        "latency_ms": payload["latency_ms"],
        "finish_reason": None,
        "cost_usd": None,
        "currency": None,
        "cost_status": "unknown",
        "cache_status": "unknown",
        "cache_mechanism": "unknown",
    }
    return {
        "call_id": payload["attempt_usage_id"],
        "attempt_usage_id": payload["attempt_usage_id"],
        "cache_observation_id": None,
        "component_id": "translation",
        "component_run_id": component["component_run_id"],
        "component_attempt_id": component["component_attempt_id"],
        "component_attempt_index": component["component_attempt_index"],
        "component_seq": component["component_seq"],
        "stage_id": stage_id,
        "agent": event.get("agent"),
        "work_id": payload["work_id"],
        "logical_request_id": payload["logical_request_id"],
        "semantic_attempt_index": payload["semantic_attempt_index"],
        "transport_retry_ordinal": payload["transport_retry_ordinal"],
        "physical_attempt_index": payload["physical_attempt_index"],
        "provider_id": payload["provider_id"],
        "source_id": payload["source_id"],
        "source_revision": payload["source_revision"],
        "requested_model_id": payload["model_id"],
        "observed_model_id": None,
        "provider_call_avoided": False,
        "finish_reason": None,
        "outcome": "transport_failed",
        "reason_code": payload["reason_code"],
        "retry_class": payload["retry_class"],
        "retry_disposition": payload["retry_disposition"],
        "usage": usage,
    }


def _empty_d2l_usage_totals() -> dict[str, Any]:
    return {
        "logical_request_count": 0,
        "accepted_result_count": 0,
        "physical_attempt_count": 0,
        "cache_observation_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": None,
        "currency": None,
        "cost_status": "unknown",
        "cache_counters": {},
    }


def _project_d2l_total(
    totals: Mapping[str, Any],
    *,
    component_run_id: str,
    component_attempt_id: int,
    snapshot_seq: int | None,
    accepted_through_component_seq: int,
    stage_id: str | None,
    sha256: str | None,
    failures: list[Mapping[str, Any]],
) -> dict[str, Any]:
    cache_counters = totals["cache_counters"]
    unknown_attempt_count = sum(
        row["usage"]["total_tokens"] is None for row in failures
    )
    token_totals: dict[str, int | None] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_input_tokens",
        "total_tokens",
    ):
        if unknown_attempt_count or totals[key] is None:
            token_totals[key] = None
        else:
            token_totals[key] = totals[key] + sum(
                row["usage"][key] for row in failures
            )

    cost_status = totals["cost_status"]
    cost_usd = totals["cost_usd"]
    currency = totals["currency"]
    if failures:
        snapshot_seq = None
        sha256 = None
        cost_usd = None
        if cost_status == "unknown":
            currency = None
        else:
            cost_status = "partial_unknown"
    return {
        "component_id": "translation",
        "component_run_id": component_run_id,
        "component_attempt_id": component_attempt_id,
        "component_attempt_index": component_attempt_id,
        "stage_id": stage_id,
        "snapshot_seq": snapshot_seq,
        "accepted_through_component_seq": accepted_through_component_seq,
        "physical_call_count": (
            totals["physical_attempt_count"] + len(failures)
        ),
        "cache_observation_count": totals["cache_observation_count"],
        "prompt_tokens": token_totals["prompt_tokens"],
        "completion_tokens": token_totals["completion_tokens"],
        "reasoning_tokens": token_totals["reasoning_tokens"],
        "cached_input_tokens": token_totals["cached_input_tokens"],
        "total_tokens": token_totals["total_tokens"],
        "cache_hit_count": cache_counters.get("hit", 0),
        "cache_miss_count": cache_counters.get("miss", 0),
        "unknown_attempt_count": unknown_attempt_count,
        "cost_status": cost_status,
        "cost_usd": cost_usd,
        "currency": currency,
        "snapshot_sha256": sha256,
    }


def _indexed_workflow_usage_total(
    typed_artifacts: list[dict[str, Any]],
    *,
    workflow_run_id: str,
) -> dict[str, Any] | None:
    summaries = []
    for item in typed_artifacts:
        body = item["body"]
        if not isinstance(body, Mapping):
            continue
        schema = body.get("schema") or body.get("schema_id")
        if schema in {
            "workflow_usage_summary_v1",
            "WorkflowUsageSummaryV1",
        }:
            summaries.append(body)
    if not summaries:
        return None
    if len(summaries) != 1:
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            "Parent package contains multiple workflow usage summaries.",
            409,
        )
    summary = summaries[0]
    if summary.get("workflow_run_id") != workflow_run_id:
        raise WorkflowReplayError(
            "workflow_usage_identity_drift",
            "Workflow usage summary belongs to another parent workflow.",
            409,
        )
    total = summary.get("workflow_total") or summary.get("totals")
    if not isinstance(total, Mapping):
        raise WorkflowReplayError(
            "workflow_usage_invalid",
            "Workflow usage summary does not expose a sealed total.",
            409,
        )
    return dict(total)


def _actions(
    entry: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    jobs_root: Path,
    evaluation_scope: Mapping[str, Any] | None,
    source_mode: str,
) -> dict[str, Any]:
    status = manifest["status"]
    translation = next(
        (
            row
            for row in manifest["components"]
            if row["component_id"] == "translation"
        ),
        None,
    )
    evaluation = next(
        (
            row
            for row in manifest["components"]
            if row["component_id"] == "evaluation"
        ),
        None,
    )
    score_blockers = []
    if source_mode != "live":
        score_blockers.append("historical_replay_read_only")
    if translation is None or translation["status"] != "succeeded":
        score_blockers.append("translation_not_ready")
    if evaluation is not None:
        score_blockers.append(
            "evaluation_already_completed"
            if evaluation["status"] == "succeeded"
            else "evaluation_already_started"
        )
    if evaluation_scope is None:
        score_blockers.append("evaluation_scope_missing")
    else:
        if evaluation_scope.get("scoring_handoff_status") != "validated":
            score_blockers.append("scoring_handoff_not_ready")
        if (
            evaluation_scope.get("settings_status") != "materialized"
            or not _is_sha256(evaluation_scope.get("settings_sha256"))
        ):
            score_blockers.append("evaluation_settings_not_materialized")
    readiness = _scoring_runtime_readiness(
        job_root=(jobs_root / str(entry["job_id"])).resolve(),
        job_id=str(entry["job_id"]),
        source_binding_sha256=str(entry["source_binding_sha256"]),
        selected_chapter_ids=list(entry.get("selected_chapter_ids") or []),
    )
    score_blockers.extend(readiness["blockers"])
    score_blockers = list(dict.fromkeys(score_blockers))
    return {
        "pause": {
            "allowed": source_mode == "live" and status == "running",
            "method": "POST",
            "href": f"/api/thesis/runs/{entry['run_id']}/pause",
        },
        "resume": {
            "allowed": (
                source_mode == "live"
                and bool(manifest["resume"]["available"])
            ),
            "method": "POST",
            "href": f"/api/thesis/runs/{entry['run_id']}/resume",
            "component_id": manifest["resume"]["component_id"],
        },
        "replay": {
            "allowed": True,
            "method": "GET",
            "href": (
                f"/api/thesis/runs/{entry['run_id']}/workflow-replay"
                "?after_seq=0&wait_ms=0"
            ),
        },
        "score": {
            "allowed": not score_blockers,
            "method": "POST",
            "href": f"/api/thesis/runs/{entry['run_id']}/score",
            "blocking_reasons": score_blockers,
            "runtime": readiness["runtime"],
        },
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _artifact_by_ref(
    artifact_index: Mapping[str, Any],
    artifact_ref: str,
) -> dict[str, Any]:
    if not isinstance(artifact_ref, str) or not artifact_ref:
        raise WorkflowReplayError(
            "workflow_artifact_ref_invalid",
            "artifact_ref must be a non-empty indexed reference.",
        )
    for artifact in artifact_index["artifacts"]:
        if artifact["binding"]["artifact_ref"] == artifact_ref:
            return artifact
    raise WorkflowReplayError(
        "workflow_artifact_not_found",
        "artifact_ref is not present in the validated parent artifact index.",
        404,
    )


def _resolve_artifact(root: Path, artifact_ref: str) -> Path:
    pure = PurePosixPath(artifact_ref)
    if pure.is_absolute() or not pure.parts or any(
        part in {"", ".", ".."} for part in pure.parts
    ):
        raise WorkflowReplayError(
            "workflow_artifact_ref_invalid",
            "artifact_ref is not a confined package-relative reference.",
        )
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise WorkflowReplayError(
            "workflow_artifact_path_invalid",
            "Indexed workflow artifact escapes the parent package.",
            409,
        ) from exc
    if candidate.is_symlink():
        raise WorkflowReplayError(
            "workflow_artifact_path_invalid",
            "Indexed workflow artifact must not be a symlink.",
            409,
        )
    return candidate


def _read_json(path: Path) -> Any:
    raw = _read_regular_bytes(path, max_bytes=MAX_PARENT_JSON_BYTES)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowReplayContractError(
            "json",
            str(path),
            "expected valid UTF-8 JSON",
        ) from exc


def _read_events(path: Path) -> list[dict[str, Any]]:
    raw = _read_regular_bytes(path, max_bytes=MAX_EVENT_STREAM_BYTES)
    result = []
    for index, line in enumerate(raw.splitlines()):
        if index >= MAX_EVENT_COUNT:
            raise WorkflowReplayContractError(
                "event_count",
                "$.events",
                f"event stream exceeds {MAX_EVENT_COUNT} rows",
            )
        if not line:
            raise WorkflowReplayContractError(
                "event_line",
                f"$.events[{index}]",
                "blank event lines are forbidden",
            )
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowReplayContractError(
                "event_json",
                f"$.events[{index}]",
                "expected valid UTF-8 JSON event",
            ) from exc
        result.append(validate_workflow_event_v1(value))
    return result


def _assert_parent_document_bounds(root: Path) -> None:
    _read_regular_bytes(
        root / "workflow_manifest.json",
        max_bytes=MAX_PARENT_JSON_BYTES,
    )
    _read_regular_bytes(
        root / "artifact_index.json",
        max_bytes=MAX_PARENT_JSON_BYTES,
    )
    _read_regular_bytes(
        root / "events.jsonl",
        max_bytes=MAX_EVENT_STREAM_BYTES,
    )


def _manifest_commit_sha256(root: Path) -> str:
    return physical_sha256(
        _read_regular_bytes(
            root / "workflow_manifest.json",
            max_bytes=MAX_PARENT_JSON_BYTES,
        )
    )


def _read_regular_bytes(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise WorkflowReplayError(
            "workflow_replay_file_invalid",
            "Workflow replay file is missing or unsafe.",
            409,
        )
    size = path.stat().st_size
    if size > max_bytes:
        raise WorkflowReplayError(
            "workflow_replay_file_too_large",
            "Workflow replay file exceeds the bounded Console read size.",
            413,
        )
    return path.read_bytes()


def _read_regular_tail_bytes(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[bytes, bool]:
    if path.is_symlink() or not path.is_file():
        raise WorkflowReplayError(
            "workflow_replay_file_invalid",
            "Workflow replay file is missing or unsafe.",
            409,
        )
    size = path.stat().st_size
    truncated = size > max_bytes
    with path.open("rb") as handle:
        if truncated:
            handle.seek(size - max_bytes)
        raw = handle.read(max_bytes)
    if not truncated:
        return raw, False
    first_newline = raw.find(b"\n")
    if first_newline < 0:
        raise WorkflowReplayError(
            "workflow_live_event_too_large",
            "A single Live Console event exceeds the bounded tail size.",
            413,
        )
    return raw[first_newline + 1 :], True


__all__ = [
    "MAX_WAIT_MS",
    "WORKFLOW_ARTIFACT_READ_SCHEMA",
    "WORKFLOW_REPLAY_READ_SCHEMA",
    "WorkflowReplayError",
    "read_workflow_artifact",
    "read_workflow_replay",
    "workflow_replay_root",
]
