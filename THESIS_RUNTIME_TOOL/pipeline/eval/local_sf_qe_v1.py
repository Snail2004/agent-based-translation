from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_number,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet


__all__ = [
    "LOCAL_SF_QE_EVIDENCE_SCHEMA_ID",
    "LOCAL_SF_QE_EVIDENCE_SCHEMA_VERSION",
    "SF_QE_MODEL_ID",
    "SF_QE_REPORT_TRANSFORM_ID",
    "LocalSfQePreparedV1",
    "load_local_sf_qe_evidence_v1",
    "persist_local_sf_qe_evidence_v1",
    "prepare_local_sf_qe_v1",
    "seal_local_sf_qe_evidence_v1",
    "validate_local_sf_qe_evidence_v1",
]


LOCAL_SF_QE_EVIDENCE_SCHEMA_ID = "LocalSfQeEvidenceV1"
LOCAL_SF_QE_EVIDENCE_SCHEMA_VERSION = "1.0.0"
SF_QE_MODEL_ID = "Unbabel/wmt22-cometkiwi-da"
SF_QE_REPORT_TRANSFORM_ID = "comet_native_0_1_times_100_v1"
_SELF_HASH_PATH = ("integrity", "artifact_sha256")
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("rows",)}),
)

BatchPredictorV1 = Callable[[Sequence[Mapping[str, str]], int], Sequence[float]]


@dataclass(frozen=True, slots=True)
class LocalSfQePersistResultV1:
    path: Path
    evidence: dict[str, Any]
    reused: bool


class LocalSfQePreparedV1:
    """A plan-ordered scorer backed by one already-computed local batch."""

    def __init__(self, evidence: Mapping[str, Any]) -> None:
        self.evidence = validate_local_sf_qe_evidence_v1(evidence)
        self._cursor = 0

    def __call__(self, source_text: str, target_text: str) -> float:
        if self._cursor >= len(self.evidence["rows"]):
            raise ContractValidationError(
                "sf_qe_exact_cover", "$", "local SF-QE scorer received an extra request"
            )
        row = self.evidence["rows"][self._cursor]
        source_sha256 = _text_sha256(source_text)
        target_sha256 = _text_sha256(target_text)
        if source_sha256 != row["source_text_sha256"] or target_sha256 != row[
            "target_text_sha256"
        ]:
            raise ContractValidationError(
                "sf_qe_request_order",
                "$",
                "local SF-QE request differs from the next sealed plan row",
            )
        self._cursor += 1
        return float(row["report_score_0_100"])

    @property
    def consumed_count(self) -> int:
        return self._cursor

    def assert_exact_cover(self) -> None:
        if self._cursor != len(self.evidence["rows"]):
            raise ContractValidationError(
                "sf_qe_exact_cover",
                "$",
                "not every sealed local SF-QE row was consumed by execution",
            )


def prepare_local_sf_qe_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    predictor: BatchPredictorV1,
    *,
    created_at: str,
    producer_code_commit: str,
    checkpoint_sha256: str,
    package_name: str,
    package_version: str,
    device: str,
    batch_size: int,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> LocalSfQePreparedV1:
    """Precompute all ready SF-QE jobs in one model-facing batch.

    The predictor receives only ``src`` and ``mt`` strings. Arm IDs, gold,
    references, existing scores, thresholds, and report policy are absent.
    """

    config = validate_evaluation_run_config(config_payload)
    plan = build_evaluation_plan(common_input, config)
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    checkpoint = require_sha256(checkpoint_sha256, path="$.checkpoint_sha256")
    package = require_string(package_name, path="$.package_name")
    package_ver = require_string(package_version, path="$.package_version")
    device_name = require_string(device, path="$.device")
    batch = require_int(batch_size, path="$.batch_size", minimum=1)
    method_rows = [row for row in config["methods"] if row["method_id"] == "sf_qe"]
    if len(method_rows) != 1:
        raise ContractValidationError(
            "sf_qe_method", "$.methods", "config must contain exactly one SF-QE method"
        )

    requests: list[dict[str, str]] = []
    identities: list[dict[str, str]] = []
    for job in plan.jobs:
        if job.method_id != "sf_qe" or job.status != "ready":
            continue
        packet = build_scorer_input_packet(
            common_input,
            plan,
            job.job_id,
            created_at=timestamp,
            producer_code_commit=commit,
        )
        source_text = packet["source"]["blocks"][0]["text"]
        target_text = packet["candidates"][0]["blocks"][0]["text"]
        if not source_text.strip() or not target_text.strip():
            raise ContractValidationError(
                "sf_qe_empty_text", "$.jobs", "ready SF-QE rows require non-empty text"
            )
        requests.append({"src": source_text, "mt": target_text})
        identities.append(
            {
                "job_id": job.job_id,
                "unit_id": job.unit_id,
                "arm_id": job.presentation_arm_ids[0],
                "packet_sha256": packet["integrity"]["packet_sha256"],
                "source_text_sha256": _text_sha256(source_text),
                "target_text_sha256": _text_sha256(target_text),
            }
        )
    if not requests:
        raise ContractValidationError(
            "sf_qe_empty_batch", "$.jobs", "evaluation plan has no ready SF-QE jobs"
        )

    wall_clock = clock or (lambda: datetime.now(timezone.utc))
    timer = monotonic or time.perf_counter
    started_at = _format_timestamp(wall_clock())
    started = timer()
    native_scores = list(predictor(copy.deepcopy(requests), batch))
    duration_ms = max(0, int(round((timer() - started) * 1000)))
    ended_at = _format_timestamp(wall_clock())
    if len(native_scores) != len(requests):
        raise ContractValidationError(
            "sf_qe_result_count",
            "$.rows",
            "local predictor result count differs from the sealed request count",
        )

    rows: list[dict[str, Any]] = []
    for index, (identity, value) in enumerate(zip(identities, native_scores)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractValidationError(
                "sf_qe_native_score_type",
                f"$.rows[{index}].native_score",
                "COMET score must be numeric",
            )
        native = float(value)
        if not math.isfinite(native) or native < 0 or native > 1:
            raise ContractValidationError(
                "sf_qe_native_score_range",
                f"$.rows[{index}].native_score",
                "wmt22-cometkiwi-da score must be finite within [0, 1]",
            )
        rows.append(
            {
                **identity,
                "native_score": native,
                "report_score_0_100": native * 100.0,
            }
        )

    draft = {
        "schema_id": LOCAL_SF_QE_EVIDENCE_SCHEMA_ID,
        "schema_version": LOCAL_SF_QE_EVIDENCE_SCHEMA_VERSION,
        "artifact_id": "local-sf-qe-" + _digest_rows(rows)[:24],
        "created_at": timestamp,
        "producer": {
            "workstream": "evaluation",
            "component": "local_sf_qe_v1",
            "component_version": "1.0.0",
            "code_commit": commit,
        },
        "binding": {
            "project_id": plan.project_id,
            "document_id": plan.document_id,
            "config_id": plan.config_id,
            "config_sha256": plan.config_sha256,
            "input_set_sha256": plan.input_set_sha256,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "method_id": "sf_qe",
            "method_version": method_rows[0]["method_version"],
        },
        "model": {
            "model_id": SF_QE_MODEL_ID,
            "checkpoint_sha256": checkpoint,
            "package_name": package,
            "package_version": package_ver,
            "device": device_name,
            "batch_size": batch,
            "score_transform_id": SF_QE_REPORT_TRANSFORM_ID,
        },
        "metering": {
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "batch_call_count": 1,
            "item_count": len(rows),
        },
        "rows": rows,
        "integrity": {"artifact_sha256": "0" * 64},
    }
    return LocalSfQePreparedV1(seal_local_sf_qe_evidence_v1(draft))


def seal_local_sf_qe_evidence_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(payload, policy=_POLICY, hash_path=_SELF_HASH_PATH)


def validate_local_sf_qe_evidence_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "artifact_id",
            "created_at",
            "producer",
            "binding",
            "model",
            "metering",
            "rows",
            "integrity",
        },
        path="$",
    )
    if root["schema_id"] != LOCAL_SF_QE_EVIDENCE_SCHEMA_ID:
        raise ContractValidationError("schema_id", "$.schema_id", "foreign schema")
    if root["schema_version"] != LOCAL_SF_QE_EVIDENCE_SCHEMA_VERSION:
        raise ContractValidationError(
            "schema_version", "$.schema_version", "foreign schema version"
        )
    normalized = {
        "schema_id": LOCAL_SF_QE_EVIDENCE_SCHEMA_ID,
        "schema_version": LOCAL_SF_QE_EVIDENCE_SCHEMA_VERSION,
        "artifact_id": require_string(root["artifact_id"], path="$.artifact_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_binding(root["binding"]),
        "model": _validate_model(root["model"]),
        "metering": _validate_metering(root["metering"]),
        "rows": _validate_rows(root["rows"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    if normalized["metering"]["item_count"] != len(normalized["rows"]):
        raise ContractValidationError(
            "sf_qe_item_count", "$.metering.item_count", "item count differs from rows"
        )
    expected_artifact_id = "local-sf-qe-" + _digest_rows(normalized["rows"])[:24]
    if normalized["artifact_id"] != expected_artifact_id:
        raise ContractValidationError(
            "artifact_id", "$.artifact_id", "artifact ID differs from sealed score rows"
        )
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_SELF_HASH_PATH):
        raise ContractValidationError(
            "artifact_hash", "$.integrity.artifact_sha256", "self-hash mismatch"
        )
    canonical = canonicalize(normalized, policy=_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical SF-QE evidence must remain an object")
    return canonical


def persist_local_sf_qe_evidence_v1(
    *, output_root: Path, evidence_payload: Mapping[str, Any]
) -> LocalSfQePersistResultV1:
    evidence = validate_local_sf_qe_evidence_v1(evidence_payload)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    relative = f"local_sf_qe/{evidence['integrity']['artifact_sha256']}.json"
    path = _contained_path(root, relative)
    encoded = _canonical_json_bytes(evidence)
    reused = not _publish_bytes_create_only(path, encoded)
    return LocalSfQePersistResultV1(path=path, evidence=evidence, reused=reused)


def load_local_sf_qe_evidence_v1(path: Path) -> dict[str, Any]:
    return validate_local_sf_qe_evidence_v1(_load_json_object(Path(path)))


def _validate_binding(value: Any) -> dict[str, str]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "project_id",
            "document_id",
            "config_id",
            "config_sha256",
            "input_set_sha256",
            "plan_id",
            "plan_sha256",
            "method_id",
            "method_version",
        },
        path=path,
    )
    return {
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
        "config_id": require_string(row["config_id"], path=f"{path}.config_id"),
        "config_sha256": require_sha256(
            row["config_sha256"], path=f"{path}.config_sha256"
        ),
        "input_set_sha256": require_sha256(
            row["input_set_sha256"], path=f"{path}.input_set_sha256"
        ),
        "plan_id": require_string(row["plan_id"], path=f"{path}.plan_id"),
        "plan_sha256": require_sha256(row["plan_sha256"], path=f"{path}.plan_sha256"),
        "method_id": require_enum(row["method_id"], {"sf_qe"}, path=f"{path}.method_id"),
        "method_version": require_string(
            row["method_version"], path=f"{path}.method_version"
        ),
    }


def _validate_model(value: Any) -> dict[str, Any]:
    path = "$.model"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "model_id",
            "checkpoint_sha256",
            "package_name",
            "package_version",
            "device",
            "batch_size",
            "score_transform_id",
        },
        path=path,
    )
    return {
        "model_id": require_enum(row["model_id"], {SF_QE_MODEL_ID}, path=f"{path}.model_id"),
        "checkpoint_sha256": require_sha256(
            row["checkpoint_sha256"], path=f"{path}.checkpoint_sha256"
        ),
        "package_name": require_string(row["package_name"], path=f"{path}.package_name"),
        "package_version": require_string(
            row["package_version"], path=f"{path}.package_version"
        ),
        "device": require_string(row["device"], path=f"{path}.device"),
        "batch_size": require_int(row["batch_size"], path=f"{path}.batch_size", minimum=1),
        "score_transform_id": require_enum(
            row["score_transform_id"],
            {SF_QE_REPORT_TRANSFORM_ID},
            path=f"{path}.score_transform_id",
        ),
    }


def _validate_metering(value: Any) -> dict[str, Any]:
    path = "$.metering"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "started_at",
            "ended_at",
            "duration_ms",
            "batch_call_count",
            "item_count",
        },
        path=path,
    )
    return {
        "started_at": require_rfc3339(row["started_at"], path=f"{path}.started_at"),
        "ended_at": require_rfc3339(row["ended_at"], path=f"{path}.ended_at"),
        "duration_ms": require_int(row["duration_ms"], path=f"{path}.duration_ms", minimum=0),
        "batch_call_count": require_int(
            row["batch_call_count"], path=f"{path}.batch_call_count", minimum=1
        ),
        "item_count": require_int(row["item_count"], path=f"{path}.item_count", minimum=1),
    }


def _validate_rows(value: Any) -> list[dict[str, Any]]:
    path = "$.rows"
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(require_list(value, path=path)):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw, path=row_path)
        require_exact_keys(
            row,
            required={
                "job_id",
                "unit_id",
                "arm_id",
                "packet_sha256",
                "source_text_sha256",
                "target_text_sha256",
                "native_score",
                "report_score_0_100",
            },
            path=row_path,
        )
        native = require_number(row["native_score"], path=f"{row_path}.native_score", minimum=0)
        report_score = require_number(
            row["report_score_0_100"], path=f"{row_path}.report_score_0_100", minimum=0
        )
        if native > 1 or report_score > 100 or report_score != native * 100.0:
            raise ContractValidationError(
                "sf_qe_score_transform",
                row_path,
                "report score must exactly equal native COMET score times 100",
            )
        result.append(
            {
                "job_id": require_string(row["job_id"], path=f"{row_path}.job_id"),
                "unit_id": require_string(row["unit_id"], path=f"{row_path}.unit_id"),
                "arm_id": require_string(row["arm_id"], path=f"{row_path}.arm_id"),
                "packet_sha256": require_sha256(
                    row["packet_sha256"], path=f"{row_path}.packet_sha256"
                ),
                "source_text_sha256": require_sha256(
                    row["source_text_sha256"], path=f"{row_path}.source_text_sha256"
                ),
                "target_text_sha256": require_sha256(
                    row["target_text_sha256"], path=f"{row_path}.target_text_sha256"
                ),
                "native_score": native,
                "report_score_0_100": report_score,
            }
        )
    if not result:
        raise ContractValidationError("empty_array", path, "SF-QE rows are required")
    require_unique([row["job_id"] for row in result], path=f"{path}.job_id")
    require_unique([row["packet_sha256"] for row in result], path=f"{path}.packet_sha256")
    return result


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"artifact_sha256"}, path=path)
    return {
        "artifact_sha256": require_sha256(
            row["artifact_sha256"], path=f"{path}.artifact_sha256"
        )
    }


def _digest_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(rows), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text_sha256(text: str) -> str:
    if not isinstance(text, str):
        raise ContractValidationError("type", "$", "SF-QE text must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ContractValidationError("timestamp", "$", "clock must return timezone-aware UTC")
    utc = value.astimezone(timezone.utc)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _contained_path(root: Path, relative_path: str) -> Path:
    candidate = (root / Path(*relative_path.split("/"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError("path_escape", str(candidate), "path escapes output root") from exc
    return candidate


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("json_encoding", "$", "evidence must be finite JSON") from exc


def _publish_bytes_create_only(path: Path, encoded: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == encoded:
            return False
        raise ContractValidationError(
            "immutable_conflict", str(path), "refusing to overwrite local SF-QE evidence"
        )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() == encoded:
                return False
            raise ContractValidationError(
                "immutable_conflict", str(path), "concurrent writer published other evidence"
            )
        except OSError as exc:
            raise ContractValidationError(
                "atomic_publish", str(path), "filesystem cannot publish evidence atomically"
            ) from exc
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
    except FileNotFoundError as exc:
        raise ContractValidationError("missing_artifact", str(path), "evidence is missing") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError("artifact_json", str(path), "evidence is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ContractValidationError("artifact_shape", str(path), "evidence root must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
