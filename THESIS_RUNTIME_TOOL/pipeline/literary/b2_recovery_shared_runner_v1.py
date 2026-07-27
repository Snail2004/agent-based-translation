"""Shared-backend runner for batched B2 registry and event recovery.

The module owns Literary request rendering, validation, authority application,
and immutable run artifacts.  It delegates every physical attempt to an
injected LiterarySharedRunnerBindingsV1 and owns no credential or transport.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b2_event_batch_v1 import (
    MAX_EVENT_BATCH_COMPONENTS_V1,
    render_event_review_batch_request_v1,
    validate_event_review_batch_response_v1,
)
from pipeline.literary.b2_recovery_batch_v1 import (
    render_registry_recovery_batch_request_v1,
    validate_registry_recovery_batch_response_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    RenderedB2RecoveryRequestV1,
    build_b2_recovery_index_v1,
    build_effective_b2_projection_v2,
    build_event_revision_ledger_v2,
    build_registry_recovery_ledger_v1,
    render_event_review_request_v2,
    validate_event_review_response_v2,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
    build_literary_code_ref_v1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_MODE_CLASSIFIED_V1,
    project_model_response_schema_v1,
)
from pipeline.literary.structured_output_policy_v1 import validate_structured_payload
from pipeline.literary.structured_prompt_reserve_v1 import structured_prompt_reserve_v1
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256
from pipeline.scripts.run_literary_b2_recovery_live_v1 import (
    _git_head,
    _load_profile,
    _read_object,
    _tree_hash,
    _write_new_json,
)


SHARED_RECOVERY_SEAL_SCHEMA_VERSION = "literary_b2_recovery_shared_seal_v1"
SHARED_RECOVERY_REPORT_SCHEMA_VERSION = "literary_b2_recovery_shared_report_v1"
REGISTRY_ROLE_ID = "literary.b2.registry_recovery"
EVENT_ROLE_ID = "literary.b2.event_review"


class B2RecoverySharedRunnerError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _request_payload(request: RenderedB2RecoveryRequestV1) -> dict[str, Any]:
    payload = asdict(request)
    payload["messages"] = list(payload["messages"])
    return payload


def _role_binding(
    *,
    runtime: LiterarySharedRunnerBindingsV1,
    role_id: str,
    response_schema: Mapping[str, Any],
) -> dict[str, Any]:
    capability = runtime.capability_for(
        role_id=role_id,
        response_schema=project_model_response_schema_v1(response_schema),
        binding_schema=response_schema,
    )
    preset = runtime.role_preset_for(role_id)
    source = runtime.api_source_for(role_id)
    return {
        "role_id": role_id,
        "preset_id": preset.preset_id,
        "preset_revision": preset.preset_revision,
        "model_id": preset.requested_model_id,
        "api_source_id": source["source_id"],
        "api_source_revision": source["source_revision"],
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "capability_binding_key": capability_binding_key(
            role_id, response_schema
        ),
        "capability_record_hash": canonical_hash(capability),
    }


def _request_reserve(
    request: RenderedB2RecoveryRequestV1,
    *,
    role_id: str,
    runtime: LiterarySharedRunnerBindingsV1,
) -> int:
    preset = runtime.role_preset_for(role_id)
    output_cap = int(preset.generation["max_output_tokens"])
    reserve = structured_prompt_reserve_v1(
        messages=request.messages,
        response_schema=request.response_schema,
        output_token_cap=output_cap,
    )
    if reserve.prompt_token_reserve > int(preset.generation["max_input_tokens"]):
        raise B2RecoverySharedRunnerError(
            f"{role_id} batch request exceeds its shared prompt cap"
        )
    return int(reserve.total_token_reserve)


def _accepted_usage(stage_dir: Path) -> dict[str, Any]:
    receipt = _read_object(
        stage_dir / "shared_attempt_receipt.json", "shared recovery receipt"
    )
    usage = receipt.get("usage")
    if not isinstance(usage, Mapping):
        raise B2RecoverySharedRunnerError(
            "shared recovery usage is unknown under a finite run cap"
        )
    values = [
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
    ]
    if not all(isinstance(value, int) and value >= 0 for value in values):
        raise B2RecoverySharedRunnerError(
            "shared recovery token usage is incomplete"
        )
    if values[0] + values[1] != values[2]:
        raise B2RecoverySharedRunnerError(
            "shared recovery token usage is inconsistent"
        )
    return dict(usage)


def _source_inputs(source: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    chapter_artifact = _read_object(
        source / "chapter_b2_artifact.json", "chapter B2 artifact"
    )
    interaction_paths = sorted((source / "interactions").glob("*/request.json"))
    if not interaction_paths:
        raise B2RecoverySharedRunnerError(
            "B2 source has no interaction requests"
        )
    requests = [
        _read_object(path, f"interaction request {path.parent.name}")
        for path in interaction_paths
    ]
    return chapter_artifact, requests


def _validated_resume_context(
    *,
    resume_root: Path | None,
    source: Path,
    source_tree_hash: str,
    index: Mapping[str, Any],
    profile_path: Path,
    profile: Mapping[str, Any],
    shared_runtime: LiterarySharedRunnerBindingsV1,
) -> dict[str, Any] | None:
    if resume_root is None:
        return None
    root = Path(resume_root).resolve()
    if not root.is_dir():
        raise B2RecoverySharedRunnerError("shared recovery resume root is absent")
    seal = _read_object(root / "run_seal.json", "shared recovery resume seal")
    seal_body = dict(seal)
    observed_hash = seal_body.pop("seal_hash", None)
    sealed_identity = seal.get("shared_runtime_identity")
    if not isinstance(sealed_identity, Mapping):
        sealed_identity = {}
    failure_path = root / "run_failure.json"
    failure = (
        _read_object(failure_path, "shared recovery failure")
        if failure_path.is_file()
        else {}
    )
    if (
        not isinstance(observed_hash, str)
        or canonical_hash(seal_body) != observed_hash
        or seal.get("schema_version") != SHARED_RECOVERY_SEAL_SCHEMA_VERSION
        or seal.get("backend_mode") != BACKEND_MODE_SHARED_V1
        or Path(str(seal.get("source_b2_root") or "")).resolve() != source
        or seal.get("source_tree_hash") != source_tree_hash
        or seal.get("source_b2_artifact_hash")
        != index["source_b2_artifact_hash"]
        or seal.get("recovery_index_hash") != index["recovery_index_hash"]
        or seal.get("profile_id") != profile["profile_id"]
        or seal.get("profile_sha256") != file_sha256(profile_path)
        or canonical_hash(sealed_identity)
        != canonical_hash(shared_runtime.identity_payload())
        or failure.get("schema_version")
        != "literary_b2_recovery_shared_failure_v1"
        or failure.get("run_seal_hash") != observed_hash
        or failure.get("retry_performed") is not False
        or failure.get("fallback_performed") is not False
        or (root / "live_report.json").is_file()
    ):
        raise B2RecoverySharedRunnerError(
            "shared recovery resume root differs from the sealed run"
        )
    return {
        "resume_root": str(root),
        "resume_tree_hash": _tree_hash(root),
        "resume_run_seal_hash": observed_hash,
        "stage_reuse_performed": False,
        "semantic_requests_replayed_as_new": True,
    }


def _component_ids(
    index: Mapping[str, Any], *, key: str, cap: int, require_nonempty: bool
) -> list[str]:
    rows = [row for row in index[key] if not row["overflow"]]
    if len(rows) > cap or (require_nonempty and not rows):
        raise B2RecoverySharedRunnerError(
            f"{key} cannot fit its sealed component cap"
        )
    return [str(row["component_id"]) for row in rows]


def _render_event_request(
    *,
    index: Mapping[str, Any],
    component_ids: Sequence[str],
    chapter_artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any] | None,
) -> tuple[RenderedB2RecoveryRequestV1, str]:
    if len(component_ids) == 1:
        return (
            render_event_review_request_v2(
                index=index,
                component_id=component_ids[0],
                chapter_artifact=chapter_artifact,
                registry_ledger=registry_ledger,
            ),
            "single",
        )
    return (
        render_event_review_batch_request_v1(
            index=index,
            component_ids=component_ids,
            chapter_artifact=chapter_artifact,
            registry_ledger=registry_ledger,
        ),
        "batch",
    )


def _preregister_event_requests(
    *,
    index: Mapping[str, Any],
    component_ids: Sequence[str],
    chapter_artifact: Mapping[str, Any],
) -> tuple[list[list[str]], list[tuple[RenderedB2RecoveryRequestV1, str]]]:
    groups: list[list[str]] = []
    requests: list[tuple[RenderedB2RecoveryRequestV1, str]] = []
    offset = 0
    while offset < len(component_ids):
        proposed = list(
            component_ids[offset : offset + MAX_EVENT_BATCH_COMPONENTS_V1]
        )
        try:
            request = _render_event_request(
                index=index,
                component_ids=proposed,
                chapter_artifact=chapter_artifact,
                registry_ledger=None,
            )
        except B2RecoveryContractError:
            if len(proposed) == 1:
                raise
            proposed = [proposed[0]]
            request = _render_event_request(
                index=index,
                component_ids=proposed,
                chapter_artifact=chapter_artifact,
                registry_ledger=None,
            )
        groups.append(proposed)
        requests.append(request)
        offset += len(proposed)
    return groups, requests


def run_b2_recovery_shared_v1(
    *,
    repo_root: Path,
    b2_root: Path,
    output_root: Path,
    profile_path: Path,
    frozen_db: Path,
    shared_runtime: LiterarySharedRunnerBindingsV1,
    resume_from_root: Path | None = None,
) -> dict[str, Any]:
    """Run one registry batch plus bounded event groups, with zero fallback."""

    source = Path(b2_root).resolve()
    output = Path(output_root).resolve()
    if output.exists():
        raise B2RecoverySharedRunnerError("shared recovery output root already exists")
    if resume_from_root is not None and output == Path(resume_from_root).resolve():
        raise B2RecoverySharedRunnerError(
            "shared recovery resume output must be a new immutable root"
        )
    frozen_before = file_sha256(frozen_db).upper()
    if frozen_before != FROZEN_DB_SHA256:
        raise B2RecoverySharedRunnerError("frozen DB differs from accepted baseline")

    profile_source = Path(profile_path).resolve()
    profile = _load_profile(profile_source)
    if profile["schema_version"] != "literary_b2_recovery_live_profile_v3":
        raise B2RecoverySharedRunnerError(
            "shared batched recovery requires the active V3 component profile"
        )
    if profile["safety"].get("event_review_contract_version") != "v2":
        raise B2RecoverySharedRunnerError(
            "shared event batch requires event authority contract V2"
        )

    source_tree_hash = _tree_hash(source)
    chapter_artifact, interaction_requests = _source_inputs(source)
    index = build_b2_recovery_index_v1(
        chapter_artifact=chapter_artifact,
        interaction_requests=interaction_requests,
    )
    if index["chapter_id"] != profile["safety"]["stop_after_chapter_id"]:
        raise B2RecoverySharedRunnerError(
            "source chapter differs from the sealed stop chapter"
        )
    resume_context = _validated_resume_context(
        resume_root=resume_from_root,
        source=source,
        source_tree_hash=source_tree_hash,
        index=index,
        profile_path=profile_source,
        profile=profile,
        shared_runtime=shared_runtime,
    )
    registry_ids = _component_ids(
        index,
        key="registry_components",
        cap=int(profile["limits"]["registry_recovery_calls"]),
        require_nonempty=False,
    )
    event_ids = _component_ids(
        index,
        key="event_components",
        cap=int(profile["limits"]["event_review_calls"]),
        require_nonempty=True,
    )
    registry_request = (
        render_registry_recovery_batch_request_v1(
            index=index, component_ids=registry_ids
        )
        if registry_ids
        else None
    )
    event_groups, base_event_requests = _preregister_event_requests(
        index=index,
        component_ids=event_ids,
        chapter_artifact=chapter_artifact,
    )
    reserve = sum(
        _request_reserve(request, role_id=EVENT_ROLE_ID, runtime=shared_runtime)
        for request, _mode in base_event_requests
    ) + (
        _request_reserve(
            registry_request, role_id=REGISTRY_ROLE_ID, runtime=shared_runtime
        )
        if registry_request is not None
        else 0
    )
    hard_cap = int(profile["limits"]["hard_visible_token_cap"])
    if reserve > hard_cap:
        raise B2RecoverySharedRunnerError(
            "shared recovery conservative reserve exceeds the hard cap"
        )

    role_bindings: dict[str, Any] = {
        "event_review_preregister": [
            {
                "ordinal": ordinal,
                "component_ids": group,
                "request_mode": mode,
                "request_fingerprint": request.request_fingerprint,
                "binding": _role_binding(
                    runtime=shared_runtime,
                    role_id=EVENT_ROLE_ID,
                    response_schema=request.response_schema,
                ),
            }
            for ordinal, (group, (request, mode)) in enumerate(
                zip(event_groups, base_event_requests, strict=True), 1
            )
        ]
    }
    if registry_request is not None:
        role_bindings["registry_recovery"] = _role_binding(
            runtime=shared_runtime,
            role_id=REGISTRY_ROLE_ID,
            response_schema=registry_request.response_schema,
        )
    seal_body = {
        "schema_version": SHARED_RECOVERY_SEAL_SCHEMA_VERSION,
        "backend_mode": BACKEND_MODE_SHARED_V1,
        "status": "sealed_before_api",
        "git_head": _git_head(Path(repo_root).resolve()),
        "output_root": str(output),
        "source_b2_root": str(source),
        "source_tree_hash": source_tree_hash,
        "source_b2_artifact_hash": index["source_b2_artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "chapter_id": index["chapter_id"],
        "profile_id": profile["profile_id"],
        "profile_sha256": file_sha256(profile_source),
        "shared_runtime_identity": shared_runtime.identity_payload(),
        "role_bindings": role_bindings,
        "registry_component_ids": registry_ids,
        "event_component_ids": event_ids,
        "registry_request_fingerprint": (
            registry_request.request_fingerprint
            if registry_request is not None
            else None
        ),
        "event_preregister_requests": [
            {
                "component_ids": group,
                "request_mode": mode,
                "request_fingerprint": request.request_fingerprint,
            }
            for group, (request, mode) in zip(
                event_groups, base_event_requests, strict=True
            )
        ],
        "limits": {
            **profile["limits"],
            "actual_shared_call_cap": (
                len(event_groups) + (1 if registry_request is not None else 0)
            ),
            "conservative_total_token_reserve": reserve,
        },
        "safety": dict(profile["safety"]),
        "frozen_db_sha256_before": frozen_before,
        "resume_policy": "new_attempt_replay_no_stage_reuse",
        "resume": resume_context,
        "application_response_cache": "disabled",
        "gold_or_oracle_loaded": False,
        "source_artifact_mutated": False,
        "production_publish_performed": False,
        "sealed_at": _now(),
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    output.mkdir(parents=True)
    _write_new_json(output / "run_seal.json", seal)
    _write_new_json(output / "recovery_index.json", index)

    usage_rows: list[dict[str, Any]] = []
    try:
        registry_decisions: Sequence[Mapping[str, Any]] = ()
        if registry_request is not None:
            stage_dir = output / "registry_recovery_batch"
            _write_new_json(stage_dir / "request.json", _request_payload(registry_request))

            def validate_registry(raw: Mapping[str, Any]) -> Mapping[str, Any]:
                validate_structured_payload(
                    raw, canonical_schema=registry_request.response_schema
                )
                return validate_registry_recovery_batch_response_v1(
                    raw,
                    index=index,
                    component_ids=registry_ids,
                    request_fingerprint=registry_request.request_fingerprint,
                )

            result = shared_runtime.execute_accepted_request(
                role_id=REGISTRY_ROLE_ID,
                stage_id=f"b2_registry_recovery_{index['chapter_id']}",
                logical_request_id=(
                    f"b2_registry_batch_{registry_request.request_fingerprint[:24]}"
                ),
                request=_request_payload(registry_request),
                schema_name="literary_b2_registry_recovery_batch_v1",
                semantic_validator=validate_registry,
                validator_ref=build_literary_code_ref_v1(
                    identifier="literary.b2.registry_recovery.validator",
                    revision="batch_v1",
                    callables=(
                        validate_structured_payload,
                        validate_registry_recovery_batch_response_v1,
                    ),
                ),
                application_contract_id=(
                    "literary.b2.registry_recovery.batch_apply_v1"
                ),
                application_contract_revision="v1",
                output_dir=stage_dir,
                model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
                additional_input_bindings=(
                    {"name": "recovery_run_seal", "sha256": seal["seal_hash"]},
                    {
                        "name": "recovery_index",
                        "sha256": index["recovery_index_hash"],
                    },
                ),
            )
            registry_batch_decision = dict(result.semantic_payload)
            usage = _accepted_usage(stage_dir)
            _write_new_json(stage_dir / "decision.json", registry_batch_decision)
            registry_decisions = registry_batch_decision["component_decisions"]
            usage_rows.append(
                {
                    "stage": "registry_recovery_batch",
                    "model": shared_runtime.role_preset_for(
                        REGISTRY_ROLE_ID
                    ).requested_model_id,
                    "usage": usage,
                    "shared_attempt_receipt_sha256": file_sha256(
                        stage_dir / "shared_attempt_receipt.json"
                    ),
                }
            )
        else:
            _write_new_json(
                output / "registry_recovery" / "skipped.json",
                {
                    "schema_version": "literary_b2_registry_recovery_skip_v1",
                    "reason": "no_registry_gap_tickets",
                    "recovery_index_hash": index["recovery_index_hash"],
                    "provider_call_performed": False,
                },
            )

        registry_ledger = build_registry_recovery_ledger_v1(
            index=index,
            decisions=registry_decisions,
            quarantined_components=(
                registry_batch_decision.get("quarantined_components") or []
                if registry_request is not None
                else []
            ),
        )
        _write_new_json(output / "registry_recovery_ledger.json", registry_ledger)

        event_decisions: list[Mapping[str, Any]] = []
        event_request_modes: list[str] = []
        for ordinal, (component_group, (base_request, base_mode)) in enumerate(
            zip(event_groups, base_event_requests, strict=True), 1
        ):
            event_request, event_request_mode = _render_event_request(
                index=index,
                component_ids=component_group,
                chapter_artifact=chapter_artifact,
                registry_ledger=registry_ledger,
            )
            if event_request_mode != base_mode:
                raise B2RecoverySharedRunnerError(
                    "event request mode drifted after registry overlay"
                )
            event_binding = _role_binding(
                runtime=shared_runtime,
                role_id=EVENT_ROLE_ID,
                response_schema=event_request.response_schema,
            )
            event_reserve = _request_reserve(
                event_request, role_id=EVENT_ROLE_ID, runtime=shared_runtime
            )
            actual_tokens = sum(
                int(row["usage"]["total_tokens"]) for row in usage_rows
            )
            remaining_reserve = sum(
                _request_reserve(
                    request, role_id=EVENT_ROLE_ID, runtime=shared_runtime
                )
                for request, _mode in base_event_requests[ordinal:]
            )
            if actual_tokens + event_reserve + remaining_reserve > hard_cap:
                raise B2RecoverySharedRunnerError(
                    "event requests would exceed the shared recovery hard cap"
                )
            event_stage_seal_body = {
                "schema_version": "literary_b2_event_shared_stage_seal_v1",
                "run_seal_hash": seal["seal_hash"],
                "stage_ordinal": ordinal,
                "component_ids": component_group,
                "request_mode": event_request_mode,
                "registry_recovery_ledger_hash": registry_ledger[
                    "registry_recovery_ledger_hash"
                ],
                "request_fingerprint": event_request.request_fingerprint,
                "preregister_response_schema_hash": canonical_hash(
                    base_request.response_schema
                ),
                "actual_response_schema_hash": canonical_hash(
                    event_request.response_schema
                ),
                "role_binding": event_binding,
                "request_conservative_reserve": event_reserve,
                "sealed_at": _now(),
            }
            event_stage_seal = {
                **event_stage_seal_body,
                "event_stage_seal_hash": canonical_hash(event_stage_seal_body),
            }
            event_dir = output / (
                f"event_review_{event_request_mode}_{ordinal:03d}"
            )
            _write_new_json(event_dir / "stage_seal.json", event_stage_seal)
            _write_new_json(
                event_dir / "request.json", _request_payload(event_request)
            )

            def validate_event(raw: Mapping[str, Any]) -> Mapping[str, Any]:
                validate_structured_payload(
                    raw, canonical_schema=event_request.response_schema
                )
                if event_request_mode == "single":
                    return validate_event_review_response_v2(
                        raw,
                        index=index,
                        component_id=component_group[0],
                        chapter_artifact=chapter_artifact,
                        registry_ledger=registry_ledger,
                        request_fingerprint=event_request.request_fingerprint,
                    )
                return validate_event_review_batch_response_v1(
                    raw,
                    index=index,
                    component_ids=component_group,
                    chapter_artifact=chapter_artifact,
                    registry_ledger=registry_ledger,
                    request_fingerprint=event_request.request_fingerprint,
                )

            event_validator_callables = (
                (validate_structured_payload, validate_event_review_response_v2)
                if event_request_mode == "single"
                else (
                    validate_structured_payload,
                    validate_event_review_batch_response_v1,
                )
            )
            event_result = shared_runtime.execute_accepted_request(
                role_id=EVENT_ROLE_ID,
                stage_id=(
                    f"b2_event_review_{index['chapter_id']}_{ordinal:03d}"
                ),
                logical_request_id=(
                    f"b2_event_{event_request_mode}_"
                    f"{event_request.request_fingerprint[:24]}"
                ),
                request=_request_payload(event_request),
                schema_name=f"literary_b2_event_review_{event_request_mode}_v1",
                semantic_validator=validate_event,
                validator_ref=build_literary_code_ref_v1(
                    identifier="literary.b2.event_review.validator",
                    revision=(
                        "single_v2"
                        if event_request_mode == "single"
                        else "batch_v1"
                    ),
                    callables=event_validator_callables,
                ),
                application_contract_id=(
                    f"literary.b2.event_review.{event_request_mode}_apply_v1"
                ),
                application_contract_revision="v1",
                output_dir=event_dir,
                model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
                additional_input_bindings=(
                    {"name": "recovery_run_seal", "sha256": seal["seal_hash"]},
                    {
                        "name": "recovery_index",
                        "sha256": index["recovery_index_hash"],
                    },
                    {
                        "name": "registry_recovery_ledger",
                        "sha256": registry_ledger[
                            "registry_recovery_ledger_hash"
                        ],
                    },
                    {
                        "name": "event_stage_seal",
                        "sha256": event_stage_seal["event_stage_seal_hash"],
                    },
                ),
            )
            event_decision = dict(event_result.semantic_payload)
            event_usage = _accepted_usage(event_dir)
            _write_new_json(event_dir / "decision.json", event_decision)
            usage_rows.append(
                {
                    "stage": f"event_review_{event_request_mode}_{ordinal:03d}",
                    "model": shared_runtime.role_preset_for(
                        EVENT_ROLE_ID
                    ).requested_model_id,
                    "usage": event_usage,
                    "shared_attempt_receipt_sha256": file_sha256(
                        event_dir / "shared_attempt_receipt.json"
                    ),
                }
            )
            event_request_modes.append(event_request_mode)
            if event_request_mode == "single":
                event_decisions.append(event_decision)
            else:
                event_decisions.extend(event_decision["component_decisions"])
        event_ledger = build_event_revision_ledger_v2(
            index=index,
            chapter_artifact=chapter_artifact,
            registry_ledger=registry_ledger,
            decisions=event_decisions,
        )
        _write_new_json(output / "event_revision_ledger.json", event_ledger)
        projection = build_effective_b2_projection_v2(
            chapter_artifact=chapter_artifact,
            index=index,
            registry_ledger=registry_ledger,
            event_ledger=event_ledger,
        )
        _write_new_json(output / "effective_b2_projection.json", projection)
    except Exception as exc:
        _write_new_json(
            output / "run_failure.json",
            {
                "schema_version": "literary_b2_recovery_shared_failure_v1",
                "run_seal_hash": seal["seal_hash"],
                "error_type": type(exc).__name__,
                "error": str(exc)[:1200],
                "completed_provider_calls": len(usage_rows),
                "usage_rows": usage_rows,
                "retry_performed": False,
                "fallback_performed": False,
                "frozen_db_sha256_after": file_sha256(frozen_db).upper(),
                "failed_at": _now(),
            },
        )
        raise

    if _tree_hash(source) != source_tree_hash:
        raise B2RecoverySharedRunnerError(
            "source B2 artifact changed during shared recovery"
        )
    frozen_after = file_sha256(frozen_db).upper()
    if frozen_after != frozen_before:
        raise B2RecoverySharedRunnerError("frozen DB changed during shared recovery")
    visible_tokens = sum(int(row["usage"]["total_tokens"]) for row in usage_rows)
    if visible_tokens > hard_cap:
        raise B2RecoverySharedRunnerError(
            "shared recovery usage exceeded the hard cap"
        )
    registry_actions = [
        row["action"] for row in registry_ledger["ticket_resolutions"]
    ]
    event_actions = [row["action"] for row in event_ledger["event_revisions"]]
    report_body = {
        "schema_version": SHARED_RECOVERY_REPORT_SCHEMA_VERSION,
        "backend_mode": BACKEND_MODE_SHARED_V1,
        "status": "complete",
        "run_seal_hash": seal["seal_hash"],
        "git_head": seal["git_head"],
        "chapter_id": index["chapter_id"],
        "source_b2_artifact_hash": index["source_b2_artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "registry_recovery_ledger_hash": registry_ledger[
            "registry_recovery_ledger_hash"
        ],
        "event_revision_ledger_hash": event_ledger["event_revision_ledger_hash"],
        "effective_projection_hash": projection["effective_projection_hash"],
        "provider_calls": len(usage_rows),
        "usage_rows": usage_rows,
        "visible_tokens": visible_tokens,
        "registry_action_counts": {
            action: registry_actions.count(action)
            for action in sorted(set(registry_actions))
        },
        "event_action_counts": {
            action: event_actions.count(action)
            for action in sorted(set(event_actions))
        },
        "event_request_modes": event_request_modes,
        "event_provider_call_count": len(event_request_modes),
        "recovered_candidate_card_count": len(
            projection["recovered_candidate_cards"]
        ),
        "effective_event_count": len(projection["interaction_events"]),
        "pending_registry_ticket_count": len(
            projection["pending_registry_tickets"]
        ),
        "pending_event_case_count": len(projection["pending_event_cases"]),
        "registry_recovery_skipped_no_tickets": registry_request is None,
        "resume": resume_context,
        "reused_stage_count": 0,
        "retry_performed": False,
        "fallback_performed": False,
        "application_response_cache": "disabled",
        "gold_or_oracle_loaded": False,
        "source_artifact_mutated": False,
        "book_global_identity_mutation_performed": False,
        "relation_phase_inference_performed": False,
        "translation_performed": False,
        "production_publish_performed": False,
        "frozen_db_sha256_after": frozen_after,
        "completed_at": _now(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_new_json(output / "live_report.json", report)
    return report


__all__ = [
    "B2RecoverySharedRunnerError",
    "EVENT_ROLE_ID",
    "REGISTRY_ROLE_ID",
    "SHARED_RECOVERY_REPORT_SCHEMA_VERSION",
    "SHARED_RECOVERY_SEAL_SCHEMA_VERSION",
    "run_b2_recovery_shared_v1",
]
