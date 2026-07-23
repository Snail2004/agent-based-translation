from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import quote

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
WORKFLOW_ARTIFACT_READ_SCHEMA = "workflow_artifact_read_v1"
WORKFLOW_SETUP_SCHEMA_ID = "WorkflowSetupV1"
WORKFLOW_PREFLIGHT_SCHEMA_ID = "WorkflowPreflightV1"
WORKFLOW_SELECTION_SCHEMA_ID = "WorkflowSetupSelectionV1"
WORKFLOW_LAUNCH_SCHEMA_ID = "WorkflowLaunchConfirmationV1"
WORKFLOW_APP_SCHEMA_VERSION = "1.0.0"
LIVE_START_ALLOWED = False
MAX_WAIT_MS = 20_000
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_PARENT_JSON_BYTES = 64 * 1024 * 1024
MAX_EVENT_STREAM_BYTES = 128 * 1024 * 1024
MAX_EVENT_COUNT = 100_000
PREFLIGHT_TTL_SECONDS = 30 * 60
MAX_ACTIVE_PREFLIGHTS = 64

SHARED_SETTINGS_ID = "shared_llm_transport_catalog_v1"
D2L_SETTINGS_ID = "d2l_workflow_settings_v1"
EVALUATION_SETTINGS_ID = "evaluation_workflow_settings_v1"
EVALUATION_ARM_ORDER = ("s0", "s1", "community", "google_nmt", "llm_lc")

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


class WorkflowReplayError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


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
    evaluation_option = _evaluation_settings_option()
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
                    else "real_five_chapter_workflow_closed_during_dec064"
                ),
            },
        ],
        "live_start_allowed": LIVE_START_ALLOWED,
        "dry_run_allowed": True,
        "shared_options": [shared_option],
        "d2l_settings_options": [d2l_option],
        "evaluation_settings_options": [evaluation_option],
        "defaults": {
            "shared_option_id": SHARED_SETTINGS_ID,
            "d2l_settings_option_id": D2L_SETTINGS_ID,
            "evaluation_settings_option_id": EVALUATION_SETTINGS_ID,
            "highlight_pair": None,
            "hard_total_token_cap": 6_000_000,
            "reserved_cost_cap_usd": None,
        },
        "constraints": {
            "selectable_fields": [
                "execution_mode",
                "chapter_ids",
                "highlight_pair",
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
                "scorers",
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
    if body["evaluation_settings_option_id"] != EVALUATION_SETTINGS_ID:
        raise WorkflowReplayError(
            "workflow_evaluation_settings_invalid",
            "evaluation_settings_id is not advertised by the server.",
        )
    available_chapters = {
        row["chapter_id"] for row in setup["chapters"] if row["selectable"]
    }
    if (
        not body["chapter_ids"]
        or len(body["chapter_ids"]) != len(set(body["chapter_ids"]))
        or not set(body["chapter_ids"]).issubset(available_chapters)
    ):
        raise WorkflowReplayError(
            "workflow_chapters_invalid",
            "chapter_ids must be a non-empty duplicate-free subset of the advertised chapters.",
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
    blocking_reasons = []
    if mode == "live" and not LIVE_START_ALLOWED:
        blocking_reasons.append("workflow_live_start_disabled")
    if mode == "live" and missing_credentials:
        blocking_reasons.append("workflow_credentials_unavailable")
    issued_at = _utc_now()
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=PREFLIGHT_TTL_SECONDS)
    ).isoformat().replace("+00:00", "Z")
    preflight_id = f"preflight_{run_id}"
    launch = {
        "script": "run_d2l_project_campaign",
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
        "evaluation_settings_id": EVALUATION_SETTINGS_ID,
        "evaluation_highlight_pair": body["highlight_pair"],
        "source_binding_sha256": preview["source_binding_sha256"],
        "campaign_config_sha256": preview["campaign_config_sha256"],
        "api_confirm_token": None,
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
            for reason in blocking_reasons
        ],
        "execution_mode": mode,
        "live_start_allowed": LIVE_START_ALLOWED,
        "start_allowed": mode == "dry_run" and not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "normalized_selection": {
            "schema_id": WORKFLOW_SELECTION_SCHEMA_ID,
            "schema_version": WORKFLOW_APP_SCHEMA_VERSION,
            "execution_mode": mode,
            "chapter_ids": list(body["chapter_ids"]),
            "shared_option_id": SHARED_SETTINGS_ID,
            "d2l_settings_option_id": D2L_SETTINGS_ID,
            "evaluation_settings_option_id": EVALUATION_SETTINGS_ID,
            "highlight_pair": body["highlight_pair"],
            "hard_total_token_cap": body["hard_total_token_cap"],
            "reserved_cost_cap_usd": body["reserved_cost_cap_usd"],
        },
        "source_summary": setup["source_package"],
        "shared_summary": _shared_settings_option(),
        "d2l_summary": _d2l_settings_option(),
        "evaluation_summary": _evaluation_settings_option(),
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
    if record.launch["mode"] == "live" or record.launch["allow_api"]:
        raise WorkflowReplayError(
            "workflow_live_start_disabled",
            "Live workflow start is disabled until the DEC-064 synthetic gate is accepted.",
            403,
        )
    return dict(record.launch)


def initialize_workflow_parent(
    *,
    jobs_root: str | Path,
    job_id: str,
    workflow_run_id: str,
    selected_chapter_ids: list[str],
    source_binding: Mapping[str, Any],
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
        return relay.validate_parent_package()
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
        "evaluation_settings_option_id",
        "highlight_pair",
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
        or value["schema_version"] != WORKFLOW_APP_SCHEMA_VERSION
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
    highlight = value["highlight_pair"]
    if highlight is not None:
        if (
            not isinstance(highlight, Mapping)
            or set(highlight)
            != {"baseline_arm_id", "candidate_arm_id"}
        ):
            raise WorkflowReplayError(
                "workflow_highlight_pair_invalid",
                "highlight_pair must name baseline_arm_id and candidate_arm_id.",
            )
        baseline = highlight["baseline_arm_id"]
        candidate = highlight["candidate_arm_id"]
        if (
            baseline not in EVALUATION_ARM_ORDER
            or candidate not in EVALUATION_ARM_ORDER
            or baseline == candidate
        ):
            raise WorkflowReplayError(
                "workflow_highlight_pair_invalid",
                "highlight_pair must contain two distinct registered arms.",
            )
    return {
        "execution_mode": mode,
        "chapter_ids": list(chapter_ids),
        "shared_option_id": _required_string(
            value["shared_option_id"], "shared_option_id"
        ),
        "d2l_settings_option_id": _required_string(
            value["d2l_settings_option_id"], "d2l_settings_option_id"
        ),
        "evaluation_settings_option_id": _required_string(
            value["evaluation_settings_option_id"],
            "evaluation_settings_option_id",
        ),
        "highlight_pair": None if highlight is None else dict(highlight),
        "hard_total_token_cap": token_cap,
        "reserved_cost_cap_usd": _nullable_positive_decimal(
            value["reserved_cost_cap_usd"]
        ),
    }


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
            "transport_retry_cap": 0,
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
        "revision": "v1",
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


def _evaluation_settings_option() -> dict[str, Any]:
    fixed_facts = {
        "arm_ids": list(EVALUATION_ARM_ORDER),
        "scorers": ["sf_qe", "sf_bt", "pj"],
        "aggregation_policy": "evaluation_benchmark_aggregation_v1",
        "report_policy": "full_run_report_v1",
        "verdict_policy": "evaluation_verdict_v1",
    }
    return {
        "option_id": EVALUATION_SETTINGS_ID,
        "label": "Five-arm Evaluation workflow",
        "revision": "v1",
        "sha256": canonical_sha256(fixed_facts),
        "enabled": True,
        "status": "registered",
        "fixed_facts": fixed_facts,
        "constraints": {
            "server_owned": True,
            "highlight_pair_arm_ids": list(EVALUATION_ARM_ORDER),
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
            )


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
        "usage": _usage_read_model(typed_artifacts),
        "source_mode": "live",
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
        "actions": _actions(entry, manifest),
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
    typed_artifacts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    rows = []
    component_snapshots = []
    workflow_summary = None
    for item in typed_artifacts:
        body = item["body"]
        schema = (
            body.get("schema")
            if isinstance(body, Mapping)
            else None
        ) or (
            body.get("schema_id")
            if isinstance(body, Mapping)
            else None
        )
        if schema in {
            "d2l_component_usage_snapshot_v1",
            "EvaluationComponentUsageSnapshotV1",
        }:
            component_snapshots.append(item)
            call_rows = body.get("call_rows") or body.get("calls") or []
            if isinstance(call_rows, list):
                rows.extend(call_rows)
        elif schema in {
            "workflow_usage_summary_v1",
            "WorkflowUsageSummaryV1",
        }:
            workflow_summary = item
    if not rows and not component_snapshots and workflow_summary is None:
        return None
    return {
        "schema_id": "WorkflowUsageReadModelV1",
        "schema_version": "1.0.0",
        "validated": True,
        "call_rows": rows,
        "component_snapshots": component_snapshots,
        "workflow_summary": workflow_summary,
    }


def _actions(
    entry: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    status = manifest["status"]
    return {
        "pause": {
            "allowed": status == "running",
            "method": "POST",
            "href": f"/api/thesis/runs/{entry['run_id']}/pause",
        },
        "resume": {
            "allowed": bool(manifest["resume"]["available"]),
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
    }


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


__all__ = [
    "MAX_WAIT_MS",
    "WORKFLOW_ARTIFACT_READ_SCHEMA",
    "WORKFLOW_REPLAY_READ_SCHEMA",
    "WorkflowReplayError",
    "read_workflow_artifact",
    "read_workflow_replay",
    "workflow_replay_root",
]
