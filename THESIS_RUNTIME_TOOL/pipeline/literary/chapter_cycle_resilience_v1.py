from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from pipeline.literary.checkpoint import canonical_hash, write_checkpoint_atomic


RESILIENCE_SCHEMA_VERSION = "literary_chapter_cycle_resilience_v1"
ATTEMPT_LEDGER_SCHEMA_VERSION = "literary_chapter_cycle_attempt_ledger_v1"
CONTRACT_REPAIR_SCHEMA_VERSION = "literary_contract_repair_directive_v1"
ROW_SOURCE_DISPOSITION_SCHEMA_VERSION = "literary_row_source_disposition_v1"

_STAGE_ROLES = {"b0", "auditor"}
_FORBIDDEN_REPAIR_TOKENS = {
    "accepted_output",
    "answer",
    "gold",
    "oracle",
    "reference_answer",
    "solution",
    "target_answer",
}
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.$\[\]-]{1,160}$")
_SAFE_REPAIR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.$\[\]-]{1,160}$")


class ChapterCycleResilienceError(RuntimeError):
    """Base error for bounded chapter-cycle resilience."""


class ResilientStageHalt(ChapterCycleResilienceError):
    """Raised after a stage is persisted as halted fail-closed."""

    def __init__(self, report: Mapping[str, Any]) -> None:
        self.report = deepcopy(dict(report))
        super().__init__(str(report.get("halt_reason") or "resilient stage halted"))


class TransportFailure(ChapterCycleResilienceError):
    """A retryable provider or connection failure."""

    def __init__(
        self,
        code: str,
        *,
        rotate_credential: bool = False,
        finish_reason: str | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = _safe_token(code, "transport failure code")
        self.rotate_credential = bool(rotate_credential)
        self.finish_reason = str(finish_reason) if finish_reason is not None else None
        self.usage = deepcopy(dict(usage)) if usage is not None else None
        super().__init__(self.code)


@dataclass(frozen=True)
class ContractIssue:
    code: str
    field_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _safe_repair_token(self.code, "issue code"))
        object.__setattr__(
            self,
            "field_path",
            _safe_repair_token(self.field_path, "issue field path"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field_path": self.field_path}


class WholeResponseContractFailure(ChapterCycleResilienceError):
    """A complete response cannot satisfy the closed output contract."""

    def __init__(self, issues: Sequence[ContractIssue]) -> None:
        rows = tuple(issues)
        if not rows:
            raise ChapterCycleResilienceError(
                "whole-response contract failure needs at least one issue"
            )
        self.issues = rows
        super().__init__(
            ",".join(f"{row.code}:{row.field_path}" for row in self.issues)
        )


class IntegrityOrLineageFailure(ChapterCycleResilienceError):
    """A non-retryable checkpoint, hash, lineage, or source-integrity failure."""

    def __init__(self, code: str) -> None:
        self.code = _safe_token(code, "integrity failure code")
        super().__init__(self.code)


class FailureClass(str, Enum):
    TRANSPORT = "transport_failure"
    WHOLE_RESPONSE_CONTRACT = "whole_response_contract_failure"
    ROW_SOURCE_OR_ALIAS = "row_source_or_alias_failure"
    INTEGRITY_OR_LINEAGE = "integrity_or_lineage_failure"
    SEMANTIC_PENDING = "semantic_pending"


class AttemptKind(str, Enum):
    PRIMARY = "primary"
    TRANSPORT_RETRY = "transport_retry"
    CONTRACT_REPAIR = "contract_repair"
    B0_MODEL_FALLBACK = "b0_model_fallback"


class SemanticStatus(str, Enum):
    ACCEPTED = "accepted"
    PENDING = "pending"


@dataclass(frozen=True)
class ModelEndpoint:
    provider: str
    model_id: str
    quota_bucket_id: str
    credential_revision: str

    def __post_init__(self) -> None:
        for field_name in (
            "provider",
            "model_id",
            "quota_bucket_id",
            "credential_revision",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ChapterCycleResilienceError(
                    f"model endpoint has empty {field_name}"
                )
            object.__setattr__(self, field_name, value)

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "quota_bucket_id": self.quota_bucket_id,
            "credential_revision": self.credential_revision,
        }


@dataclass(frozen=True)
class StageRequest:
    request_fingerprint: str
    semantic_payload_hash: str
    response_schema_hash: str
    prompt_version: str
    payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "request_fingerprint",
            "semantic_payload_hash",
            "response_schema_hash",
            "prompt_version",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ChapterCycleResilienceError(
                    f"stage request has empty {field_name}"
                )
            object.__setattr__(self, field_name, value)
        if not isinstance(self.payload, Mapping):
            raise ChapterCycleResilienceError("stage request payload must be a mapping")
        object.__setattr__(self, "payload", deepcopy(dict(self.payload)))


@dataclass(frozen=True)
class ContractRepairDirective:
    issues: tuple[ContractIssue, ...]
    directive_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTRACT_REPAIR_SCHEMA_VERSION,
            "instruction": (
                "Return one complete JSON object matching the supplied response "
                "schema. Correct only the listed structural contract violations. "
                "Do not add prose outside the JSON object."
            ),
            "issues": [row.to_dict() for row in self.issues],
            "directive_hash": self.directive_hash,
        }


@dataclass(frozen=True)
class AttemptPlan:
    attempt_number: int
    attempt_kind: AttemptKind
    logical_request_kind: AttemptKind
    endpoint: ModelEndpoint
    request: StageRequest
    effective_request_fingerprint: str
    repair_directive: ContractRepairDirective | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "attempt_kind": self.attempt_kind.value,
            "logical_request_kind": self.logical_request_kind.value,
            "endpoint": self.endpoint.to_dict(),
            "request_fingerprint": self.request.request_fingerprint,
            "effective_request_fingerprint": self.effective_request_fingerprint,
            "semantic_payload_hash": self.request.semantic_payload_hash,
            "response_schema_hash": self.request.response_schema_hash,
            "prompt_version": self.request.prompt_version,
            "repair_directive": (
                self.repair_directive.to_dict()
                if self.repair_directive is not None
                else None
            ),
        }


@dataclass(frozen=True)
class StageResponse:
    parsed_payload: Any
    model_actual: str | None = None
    usage: Mapping[str, Any] | None = None
    finish_reason: str | None = None
    artifact_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.usage is not None and not isinstance(self.usage, Mapping):
            raise ChapterCycleResilienceError("stage response usage must be a mapping")
        if self.usage is not None:
            object.__setattr__(self, "usage", deepcopy(dict(self.usage)))
        object.__setattr__(
            self,
            "artifact_paths",
            tuple(str(path) for path in self.artifact_paths),
        )


@dataclass(frozen=True)
class ValidationOutcome:
    semantic_status: SemanticStatus
    result_hash: str
    artifact_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        value = str(self.result_hash or "").strip()
        if not value:
            raise ChapterCycleResilienceError("validation outcome needs a result hash")
        object.__setattr__(self, "result_hash", value)
        object.__setattr__(
            self,
            "artifact_paths",
            tuple(str(path) for path in self.artifact_paths),
        )


@dataclass(frozen=True)
class ResiliencePolicy:
    max_transport_retries_per_request: int = 2
    max_contract_repairs: int = 1
    b0_contract_fallback_enabled: bool = True
    b0_fallback_model_id: str = "gpt-5.4"

    def __post_init__(self) -> None:
        if not 0 <= self.max_transport_retries_per_request <= 5:
            raise ChapterCycleResilienceError(
                "transport retries must be between zero and five"
            )
        if not 0 <= self.max_contract_repairs <= 1:
            raise ChapterCycleResilienceError(
                "contract repairs must be zero or one"
            )
        if not isinstance(self.b0_contract_fallback_enabled, bool):
            raise ChapterCycleResilienceError(
                "B0 contract fallback enabled must be bool"
            )
        if not str(self.b0_fallback_model_id or "").strip():
            raise ChapterCycleResilienceError("B0 fallback model id is empty")


@dataclass(frozen=True)
class StageExecutionSpec:
    stage_id: str
    chapter_id: str
    stage_role: str
    request: StageRequest
    primary_endpoints: tuple[ModelEndpoint, ...]
    output_dir: Path
    b0_fallback_endpoints: tuple[ModelEndpoint, ...] = ()
    protected_checkpoint_pointer: Path | None = None

    def __post_init__(self) -> None:
        for field_name in ("stage_id", "chapter_id", "stage_role"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ChapterCycleResilienceError(
                    f"stage execution spec has empty {field_name}"
                )
            object.__setattr__(self, field_name, value)
        if self.stage_role not in _STAGE_ROLES:
            raise ChapterCycleResilienceError("stage role must be b0 or auditor")
        primary = tuple(self.primary_endpoints)
        fallback = tuple(self.b0_fallback_endpoints)
        if not primary:
            raise ChapterCycleResilienceError("stage needs a primary endpoint")
        _verify_endpoint_family(primary, "primary")
        if self.stage_role == "auditor" and fallback:
            raise ChapterCycleResilienceError(
                "Auditor stages cannot configure a model fallback"
            )
        if fallback:
            _verify_endpoint_family(fallback, "B0 fallback")
        object.__setattr__(self, "primary_endpoints", primary)
        object.__setattr__(self, "b0_fallback_endpoints", fallback)
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        if self.protected_checkpoint_pointer is not None:
            object.__setattr__(
                self,
                "protected_checkpoint_pointer",
                Path(self.protected_checkpoint_pointer),
            )


@dataclass(frozen=True)
class RowSourceIssue:
    row_id: str
    row_kind: str
    field_path: str
    source_block_ids: tuple[str, ...]
    reason_code: str
    load_bearing_for_identity: bool

    def __post_init__(self) -> None:
        for field_name in ("row_id", "row_kind", "field_path", "reason_code"):
            object.__setattr__(
                self,
                field_name,
                _safe_token(getattr(self, field_name), field_name),
            )
        source_ids = tuple(
            _safe_token(block_id, "source block id")
            for block_id in self.source_block_ids
        )
        if not source_ids:
            raise ChapterCycleResilienceError(
                "row source issue needs at least one source block"
            )
        object.__setattr__(self, "source_block_ids", source_ids)


InvokeStage = Callable[[AttemptPlan, Path], StageResponse]
ValidateStage = Callable[
    [StageResponse, AttemptPlan, Path],
    ValidationOutcome,
]


def build_contract_repair_directive(
    failure: WholeResponseContractFailure,
) -> ContractRepairDirective:
    issues = tuple(failure.issues)
    body = {
        "schema_version": CONTRACT_REPAIR_SCHEMA_VERSION,
        "instruction": (
            "Return one complete JSON object matching the supplied response "
            "schema. Correct only the listed structural contract violations. "
            "Do not add prose outside the JSON object."
        ),
        "issues": [row.to_dict() for row in issues],
    }
    return ContractRepairDirective(
        issues=issues,
        directive_hash=canonical_hash(body),
    )


def route_row_source_issue(issue: RowSourceIssue) -> dict[str, Any]:
    body = {
        "schema_version": ROW_SOURCE_DISPOSITION_SCHEMA_VERSION,
        "failure_class": FailureClass.ROW_SOURCE_OR_ALIAS.value,
        "row_id": issue.row_id,
        "row_kind": issue.row_kind,
        "field_path": issue.field_path,
        "source_block_ids": list(issue.source_block_ids),
        "reason_code": issue.reason_code,
        "load_bearing_for_identity": issue.load_bearing_for_identity,
        "row_action": (
            "retain_entity_pending"
            if issue.load_bearing_for_identity
            else "exclude_defective_row"
        ),
        "resulting_status": (
            "pending" if issue.load_bearing_for_identity else "row_downscoped"
        ),
        "authority_effect": "none",
        "production_publish_performed": False,
    }
    return {**body, "disposition_hash": canonical_hash(body)}


def execute_resilient_stage(
    *,
    spec: StageExecutionSpec,
    invoke: InvokeStage,
    validate: ValidateStage,
    policy: ResiliencePolicy | None = None,
) -> dict[str, Any]:
    selected_policy = policy or ResiliencePolicy()
    _verify_fallback_policy(spec, selected_policy)
    output_dir = spec.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pointer_before = _path_snapshot(spec.protected_checkpoint_pointer)

    attempts: list[dict[str, Any]] = []
    transport_failures_by_request: dict[str, int] = {}
    contract_repair_used = False
    fallback_used = False
    logical_kind = AttemptKind.PRIMARY
    attempt_kind = AttemptKind.PRIMARY
    endpoint_family = spec.primary_endpoints
    endpoint_index = 0
    repair_directive: ContractRepairDirective | None = None

    while True:
        attempt_number = len(attempts) + 1
        endpoint = endpoint_family[endpoint_index]
        effective_fingerprint = _effective_request_fingerprint(
            spec.request,
            repair_directive=repair_directive,
        )
        plan = AttemptPlan(
            attempt_number=attempt_number,
            attempt_kind=attempt_kind,
            logical_request_kind=logical_kind,
            endpoint=endpoint,
            request=spec.request,
            effective_request_fingerprint=effective_fingerprint,
            repair_directive=repair_directive,
        )
        attempt_dir = (
            output_dir
            / "attempts"
            / f"{attempt_number:03d}_{attempt_kind.value}"
        )
        attempt_dir.mkdir(parents=True, exist_ok=False)
        started_at = _now()
        response: StageResponse | None = None

        try:
            response = invoke(plan, attempt_dir)
            response = _with_persisted_stage_response(
                response,
                attempt_dir=attempt_dir,
            )
            outcome = validate(response, plan, attempt_dir)
            _assert_pointer_unchanged(
                spec.protected_checkpoint_pointer,
                pointer_before,
            )
            record = _attempt_record(
                spec=spec,
                plan=plan,
                started_at=started_at,
                status="accepted",
                response=response,
                failure_class=(
                    FailureClass.SEMANTIC_PENDING.value
                    if outcome.semantic_status is SemanticStatus.PENDING
                    else None
                ),
                error_code=None,
                artifact_paths=(
                    *response.artifact_paths,
                    *outcome.artifact_paths,
                ),
                result_hash=outcome.result_hash,
            )
            attempts.append(record)
            _persist_attempt(output_dir, attempt_dir, record, spec, attempts)
            return _success_report(
                spec=spec,
                attempts=attempts,
                outcome=outcome,
                model_actual=response.model_actual or endpoint.model_id,
                pointer_before=pointer_before,
            )
        except TransportFailure as exc:
            record = _attempt_record(
                spec=spec,
                plan=plan,
                started_at=started_at,
                status="failed",
                response=response,
                failure_class=FailureClass.TRANSPORT.value,
                error_code=exc.code,
                finish_reason=exc.finish_reason,
                usage=exc.usage,
            )
            attempts.append(record)
            _persist_attempt(output_dir, attempt_dir, record, spec, attempts)
            transport_key = "|".join(
                (
                    effective_fingerprint,
                    endpoint.provider,
                    endpoint.model_id,
                )
            )
            count = transport_failures_by_request.get(transport_key, 0) + 1
            transport_failures_by_request[transport_key] = count
            if count <= selected_policy.max_transport_retries_per_request:
                if exc.rotate_credential and len(endpoint_family) > 1:
                    endpoint_index = (endpoint_index + 1) % len(endpoint_family)
                attempt_kind = AttemptKind.TRANSPORT_RETRY
                continue
            return _halt(
                spec=spec,
                attempts=attempts,
                failure_class=FailureClass.TRANSPORT.value,
                halt_reason="transport_retry_exhausted",
                pointer_before=pointer_before,
            )
        except WholeResponseContractFailure as exc:
            record = _attempt_record(
                spec=spec,
                plan=plan,
                started_at=started_at,
                status="failed",
                response=response,
                failure_class=FailureClass.WHOLE_RESPONSE_CONTRACT.value,
                error_code="response_contract_rejected",
                contract_issues=[row.to_dict() for row in exc.issues],
            )
            attempts.append(record)
            _persist_attempt(output_dir, attempt_dir, record, spec, attempts)
            if (
                not contract_repair_used
                and selected_policy.max_contract_repairs > 0
            ):
                contract_repair_used = True
                repair_directive = build_contract_repair_directive(exc)
                logical_kind = AttemptKind.CONTRACT_REPAIR
                attempt_kind = AttemptKind.CONTRACT_REPAIR
                continue
            if (
                spec.stage_role == "b0"
                and selected_policy.b0_contract_fallback_enabled
                and spec.b0_fallback_endpoints
                and not fallback_used
            ):
                fallback_used = True
                logical_kind = AttemptKind.B0_MODEL_FALLBACK
                attempt_kind = AttemptKind.B0_MODEL_FALLBACK
                endpoint_family = spec.b0_fallback_endpoints
                endpoint_index = 0
                continue
            return _halt(
                spec=spec,
                attempts=attempts,
                failure_class=FailureClass.WHOLE_RESPONSE_CONTRACT.value,
                halt_reason="response_contract_repair_exhausted",
                pointer_before=pointer_before,
            )
        except IntegrityOrLineageFailure as exc:
            record = _attempt_record(
                spec=spec,
                plan=plan,
                started_at=started_at,
                status="failed",
                response=response,
                failure_class=FailureClass.INTEGRITY_OR_LINEAGE.value,
                error_code=exc.code,
            )
            attempts.append(record)
            _persist_attempt(output_dir, attempt_dir, record, spec, attempts)
            return _halt(
                spec=spec,
                attempts=attempts,
                failure_class=FailureClass.INTEGRITY_OR_LINEAGE.value,
                halt_reason=exc.code,
                pointer_before=pointer_before,
            )
        except Exception as exc:
            record = _attempt_record(
                spec=spec,
                plan=plan,
                started_at=started_at,
                status="failed",
                response=response,
                failure_class=None,
                error_code=f"unclassified:{type(exc).__name__}",
            )
            attempts.append(record)
            _persist_attempt(output_dir, attempt_dir, record, spec, attempts)
            return _halt(
                spec=spec,
                attempts=attempts,
                failure_class=None,
                halt_reason="unclassified_failure",
                pointer_before=pointer_before,
            )


def _safe_token(value: Any, label: str) -> str:
    token = str(value or "").strip()
    if not _SAFE_TOKEN_RE.fullmatch(token):
        raise ChapterCycleResilienceError(f"{label} is malformed")
    return token


def _safe_repair_token(value: Any, label: str) -> str:
    token = str(value or "").strip()
    if not _SAFE_REPAIR_TOKEN_RE.fullmatch(token):
        raise ChapterCycleResilienceError(f"{label} is malformed")
    lowered = token.casefold()
    if any(part in lowered for part in _FORBIDDEN_REPAIR_TOKENS):
        raise ChapterCycleResilienceError(
            f"{label} contains answer-shaped repair material"
        )
    return token


def _verify_endpoint_family(
    endpoints: Sequence[ModelEndpoint],
    label: str,
) -> None:
    providers = {row.provider for row in endpoints}
    models = {row.model_id for row in endpoints}
    if len(providers) != 1 or len(models) != 1:
        raise ChapterCycleResilienceError(
            f"{label} endpoints must keep one provider and one model"
        )


def _verify_fallback_policy(
    spec: StageExecutionSpec,
    policy: ResiliencePolicy,
) -> None:
    if not spec.b0_fallback_endpoints:
        return
    if not policy.b0_contract_fallback_enabled:
        raise ChapterCycleResilienceError(
            "B0 fallback endpoints are configured while fallback is disabled"
        )
    models = {row.model_id for row in spec.b0_fallback_endpoints}
    if models != {policy.b0_fallback_model_id}:
        raise ChapterCycleResilienceError(
            "B0 fallback endpoints must use the locked fallback model"
        )


def _effective_request_fingerprint(
    request: StageRequest,
    *,
    repair_directive: ContractRepairDirective | None,
) -> str:
    if repair_directive is None:
        return request.request_fingerprint
    return canonical_hash(
        {
            "request_fingerprint": request.request_fingerprint,
            "semantic_payload_hash": request.semantic_payload_hash,
            "response_schema_hash": request.response_schema_hash,
            "prompt_version": request.prompt_version,
            "repair_directive_hash": repair_directive.directive_hash,
        }
    )


def _path_snapshot(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = Path(path)
    if not resolved.exists():
        return {"exists": False, "sha256": None}
    if not resolved.is_file():
        raise ChapterCycleResilienceError(
            "protected checkpoint pointer is not a file"
        )
    return {"exists": True, "sha256": _file_sha256(resolved)}


def _assert_pointer_unchanged(
    path: Path | None,
    before: Mapping[str, Any] | None,
) -> None:
    if _path_snapshot(path) != before:
        raise IntegrityOrLineageFailure("protected_checkpoint_pointer_changed")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _attempt_record(
    *,
    spec: StageExecutionSpec,
    plan: AttemptPlan,
    started_at: str,
    status: str,
    response: StageResponse | None,
    failure_class: str | None,
    error_code: str | None,
    finish_reason: str | None = None,
    usage: Mapping[str, Any] | None = None,
    artifact_paths: Sequence[str] = (),
    result_hash: str | None = None,
    contract_issues: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    observed_usage = usage
    observed_finish = finish_reason
    observed_artifacts = tuple(artifact_paths)
    if response is not None:
        if observed_usage is None:
            observed_usage = response.usage
        if observed_finish is None:
            observed_finish = response.finish_reason
        if not observed_artifacts:
            observed_artifacts = response.artifact_paths
    body = {
        "schema_version": "literary_chapter_cycle_attempt_v1",
        "stage_id": spec.stage_id,
        "chapter_id": spec.chapter_id,
        **plan.to_dict(),
        "status": status,
        "failure_class": failure_class,
        "error_code": error_code,
        "contract_issues": [deepcopy(dict(row)) for row in contract_issues],
        "model_actual": (
            response.model_actual
            if response is not None and response.model_actual
            else plan.endpoint.model_id
        ),
        "usage": deepcopy(dict(observed_usage)) if observed_usage is not None else None,
        "finish_reason": observed_finish,
        "artifact_paths": sorted(set(str(path) for path in observed_artifacts)),
        "result_hash": result_hash,
        "started_at_utc": started_at,
        "finished_at_utc": _now(),
        "production_publish_performed": False,
    }
    return {**body, "attempt_hash": canonical_hash(body)}


def _with_persisted_stage_response(
    response: StageResponse,
    *,
    attempt_dir: Path,
) -> StageResponse:
    body = {
        "schema_version": "literary_chapter_cycle_stage_response_v1",
        "parsed_payload": deepcopy(response.parsed_payload),
        "model_actual": response.model_actual,
        "usage": deepcopy(dict(response.usage)) if response.usage is not None else None,
        "finish_reason": response.finish_reason,
        "adapter_artifact_paths": list(response.artifact_paths),
    }
    artifact = {**body, "stage_response_hash": canonical_hash(body)}
    path = attempt_dir / "stage_response.json"
    _write_immutable_json(path, artifact)
    return StageResponse(
        parsed_payload=response.parsed_payload,
        model_actual=response.model_actual,
        usage=response.usage,
        finish_reason=response.finish_reason,
        artifact_paths=(str(path), *response.artifact_paths),
    )


def _persist_attempt(
    output_dir: Path,
    attempt_dir: Path,
    record: Mapping[str, Any],
    spec: StageExecutionSpec,
    attempts: Sequence[Mapping[str, Any]],
) -> None:
    _write_immutable_json(attempt_dir / "attempt.json", record)
    body = {
        "schema_version": ATTEMPT_LEDGER_SCHEMA_VERSION,
        "stage_id": spec.stage_id,
        "chapter_id": spec.chapter_id,
        "request_fingerprint": spec.request.request_fingerprint,
        "semantic_payload_hash": spec.request.semantic_payload_hash,
        "response_schema_hash": spec.request.response_schema_hash,
        "attempts": [deepcopy(dict(row)) for row in attempts],
        "latest_attempt_number": len(attempts),
        "production_publish_performed": False,
    }
    ledger = {**body, "attempt_ledger_hash": canonical_hash(body)}
    generation_path = (
        output_dir
        / "attempt_ledger_generations"
        / f"{len(attempts):03d}_{ledger['attempt_ledger_hash'][:12]}.json"
    )
    _write_immutable_json(generation_path, ledger)
    write_checkpoint_atomic(output_dir / "attempt_ledger.json", ledger)


def _success_report(
    *,
    spec: StageExecutionSpec,
    attempts: Sequence[Mapping[str, Any]],
    outcome: ValidationOutcome,
    model_actual: str,
    pointer_before: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body = {
        "schema_version": RESILIENCE_SCHEMA_VERSION,
        "status": (
            "accepted_semantic_pending"
            if outcome.semantic_status is SemanticStatus.PENDING
            else "accepted"
        ),
        "stage_id": spec.stage_id,
        "chapter_id": spec.chapter_id,
        "stage_role": spec.stage_role,
        "request_fingerprint": spec.request.request_fingerprint,
        "semantic_payload_hash": spec.request.semantic_payload_hash,
        "response_schema_hash": spec.request.response_schema_hash,
        "prompt_version": spec.request.prompt_version,
        "attempt_count": len(attempts),
        "attempt_hashes": [row["attempt_hash"] for row in attempts],
        "model_actual": model_actual,
        "result_hash": outcome.result_hash,
        "semantic_status": outcome.semantic_status.value,
        "protected_checkpoint_pointer_before": deepcopy(pointer_before),
        "protected_checkpoint_pointer_after": _path_snapshot(
            spec.protected_checkpoint_pointer
        ),
        "production_publish_performed": False,
        "completed_at_utc": _now(),
    }
    report = {**body, "resilience_report_hash": canonical_hash(body)}
    write_checkpoint_atomic(spec.output_dir / "resilience_report.json", report)
    return report


def _halt(
    *,
    spec: StageExecutionSpec,
    attempts: Sequence[Mapping[str, Any]],
    failure_class: str | None,
    halt_reason: str,
    pointer_before: Mapping[str, Any] | None,
) -> dict[str, Any]:
    pointer_after = _path_snapshot(spec.protected_checkpoint_pointer)
    if pointer_after != pointer_before:
        failure_class = FailureClass.INTEGRITY_OR_LINEAGE.value
        halt_reason = "protected_checkpoint_pointer_changed"
    body = {
        "schema_version": RESILIENCE_SCHEMA_VERSION,
        "status": "halted_fail_closed",
        "stage_id": spec.stage_id,
        "chapter_id": spec.chapter_id,
        "stage_role": spec.stage_role,
        "request_fingerprint": spec.request.request_fingerprint,
        "semantic_payload_hash": spec.request.semantic_payload_hash,
        "response_schema_hash": spec.request.response_schema_hash,
        "prompt_version": spec.request.prompt_version,
        "attempt_count": len(attempts),
        "attempt_hashes": [row["attempt_hash"] for row in attempts],
        "failure_class": failure_class,
        "halt_reason": halt_reason,
        "protected_checkpoint_pointer_before": deepcopy(pointer_before),
        "protected_checkpoint_pointer_after": pointer_after,
        "production_publish_performed": False,
        "failed_at_utc": _now(),
    }
    report = {**body, "resilience_report_hash": canonical_hash(body)}
    write_checkpoint_atomic(spec.output_dir / "resilience_failure.json", report)
    raise ResilientStageHalt(report)


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing != payload:
            raise IntegrityOrLineageFailure("immutable_attempt_artifact_differs")
        return
    write_checkpoint_atomic(target, dict(payload))


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = [
    "AttemptKind",
    "AttemptPlan",
    "ChapterCycleResilienceError",
    "ContractIssue",
    "ContractRepairDirective",
    "FailureClass",
    "IntegrityOrLineageFailure",
    "ModelEndpoint",
    "ResiliencePolicy",
    "ResilientStageHalt",
    "RowSourceIssue",
    "SemanticStatus",
    "StageExecutionSpec",
    "StageRequest",
    "StageResponse",
    "TransportFailure",
    "ValidationOutcome",
    "WholeResponseContractFailure",
    "build_contract_repair_directive",
    "execute_resilient_stage",
    "route_row_source_issue",
]
