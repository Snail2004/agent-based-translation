from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping, Protocol, Sequence

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.cometkiwi_subprocess_v1 import (
    validate_cometkiwi_batch_response_v1,
    validate_cometkiwi_runtime_description_v1,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.live_pilot_preflight_v1 import (
    validate_evaluation_live_pilot_preflight_binding,
)
from pipeline.eval.local_sf_qe_v1 import SF_QE_MODEL_ID, SF_QE_REPORT_TRANSFORM_ID
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet


__all__ = [
    "PILOT_LOCAL_SF_QE_BINDING_SCHEMA_ID",
    "PilotLocalSfQePreparedV1",
    "prepare_evaluation_live_pilot_sf_qe_v1",
    "validate_pilot_local_sf_qe_binding_v1",
]


PILOT_LOCAL_SF_QE_BINDING_SCHEMA_ID = "PilotLocalSfQeBindingV1"


class PilotCometKiwiPredictorV1(Protocol):
    @property
    def checkpoint_sha256(self) -> str: ...

    def describe_runtime(self) -> Mapping[str, str]: ...

    def __call__(
        self, rows: Sequence[Mapping[str, str]], batch_size: int
    ) -> Sequence[float]: ...


class PilotLocalSfQePreparedV1:
    def __init__(
        self, *, rows: Sequence[Mapping[str, Any]], execution_binding: Mapping[str, Any]
    ) -> None:
        self._rows = tuple(copy.deepcopy(dict(row)) for row in rows)
        self._binding = validate_pilot_local_sf_qe_binding_v1(execution_binding)
        if len(self._rows) != self._binding["selected_job_count"]:
            raise ContractValidationError(
                "sf_qe_binding_count",
                "$.local_sf_qe.selected_job_count",
                "local SF-QE binding count differs from prepared rows",
            )
        self._cursor = 0

    @property
    def execution_binding(self) -> dict[str, Any]:
        return copy.deepcopy(self._binding)

    def begin_execution(self) -> None:
        if self._cursor not in {0, len(self._rows)}:
            raise ContractValidationError(
                "sf_qe_exact_cover",
                "$",
                "cannot restart a partially consumed local SF-QE batch",
            )
        self._cursor = 0

    def __call__(self, source_text: str, target_text: str) -> float:
        if self._cursor >= len(self._rows):
            raise ContractValidationError(
                "sf_qe_exact_cover", "$", "pilot local SF-QE received an extra row"
            )
        row = self._rows[self._cursor]
        if (
            _text_sha256(source_text) != row["source_text_sha256"]
            or _text_sha256(target_text) != row["target_text_sha256"]
        ):
            raise ContractValidationError(
                "sf_qe_request_order",
                "$",
                "pilot local SF-QE request differs from its sealed batch order",
            )
        self._cursor += 1
        return float(row["report_score_0_100"])

    def assert_exact_cover(self) -> None:
        if self._cursor != len(self._rows):
            raise ContractValidationError(
                "sf_qe_exact_cover", "$", "pilot did not consume every local SF-QE row"
            )


def prepare_evaluation_live_pilot_sf_qe_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    predictor: PilotCometKiwiPredictorV1,
    *,
    batch_size: int,
) -> PilotLocalSfQePreparedV1:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ContractValidationError("type", "$.batch_size", "batch size must be an integer")
    if batch_size < 1 or batch_size > 512:
        raise ContractValidationError(
            "range", "$.batch_size", "batch size must be within 1..512"
        )
    config = validate_evaluation_run_config(config_payload)
    preflight = validate_evaluation_live_pilot_preflight_binding(
        preflight_payload, common_input, config
    )
    plan = build_evaluation_plan(common_input, config)
    jobs_by_id = {row.job_id: row for row in plan.jobs}
    requests: list[dict[str, str]] = []
    identities: list[dict[str, str]] = []
    for preflight_job in preflight["jobs"]:
        if preflight_job["method_id"] != "sf_qe":
            continue
        job = jobs_by_id[preflight_job["job_id"]]
        packet = build_scorer_input_packet(
            common_input,
            plan,
            job.job_id,
            created_at=preflight["created_at"],
            producer_code_commit=preflight["producer"]["code_commit"],
        )
        if packet["integrity"]["packet_sha256"] != preflight_job["packet_sha256"]:
            raise ContractValidationError(
                "pilot_packet_binding",
                "$.preflight.jobs",
                "local SF-QE packet differs from the sealed preflight",
            )
        source_text = packet["source"]["blocks"][0]["text"]
        target_text = packet["candidates"][0]["blocks"][0]["text"]
        requests.append({"src": source_text, "mt": target_text})
        identities.append(
            {
                "packet_sha256": packet["integrity"]["packet_sha256"],
                "source_text_sha256": _text_sha256(source_text),
                "target_text_sha256": _text_sha256(target_text),
            }
        )
    if not requests:
        raise ContractValidationError(
            "sf_qe_empty_batch", "$.preflight.jobs", "pilot has no ready SF-QE jobs"
        )

    runtime = validate_cometkiwi_runtime_description_v1(
        predictor.describe_runtime(),
        expected_checkpoint_sha256=predictor.checkpoint_sha256,
    )
    native_scores = list(predictor(copy.deepcopy(requests), batch_size))
    validated_scores = validate_cometkiwi_batch_response_v1(
        {"schema_id": "CometKiwiBatchResponseV1", "scores": native_scores},
        expected_count=len(requests),
    )["scores"]
    rows = [
        {
            **identity,
            "native_score": score,
            "report_score_0_100": score * 100.0,
        }
        for identity, score in zip(identities, validated_scores, strict=True)
    ]
    binding = validate_pilot_local_sf_qe_binding_v1(
        {
            "schema_id": PILOT_LOCAL_SF_QE_BINDING_SCHEMA_ID,
            "model_id": SF_QE_MODEL_ID,
            "report_transform_id": SF_QE_REPORT_TRANSFORM_ID,
            "checkpoint_sha256": runtime["checkpoint_sha256"],
            "package_name": runtime["package_name"],
            "package_version": runtime["package_version"],
            "python_version": runtime["python_version"],
            "device": runtime["device"],
            "batch_size": batch_size,
            "selected_job_count": len(rows),
            "packet_set_sha256": _digest_json(
                [row["packet_sha256"] for row in rows]
            ),
            "score_set_sha256": _digest_json(
                [row["report_score_0_100"] for row in rows]
            ),
        }
    )
    return PilotLocalSfQePreparedV1(rows=rows, execution_binding=binding)


def validate_pilot_local_sf_qe_binding_v1(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError("type", "$.local_sf_qe", "expected an object")
    row = copy.deepcopy(dict(value))
    required = {
        "schema_id",
        "model_id",
        "report_transform_id",
        "checkpoint_sha256",
        "package_name",
        "package_version",
        "python_version",
        "device",
        "batch_size",
        "selected_job_count",
        "packet_set_sha256",
        "score_set_sha256",
    }
    if set(row) != required:
        raise ContractValidationError(
            "closed_schema", "$.local_sf_qe", "local SF-QE binding fields differ"
        )
    result: dict[str, Any] = {}
    for field in (
        "schema_id",
        "model_id",
        "report_transform_id",
        "package_name",
        "package_version",
        "python_version",
        "device",
    ):
        value = row[field]
        if not isinstance(value, str) or not value.strip():
            raise ContractValidationError(
                "string", f"$.local_sf_qe.{field}", "expected a nonempty string"
            )
        result[field] = value
    if result["schema_id"] != PILOT_LOCAL_SF_QE_BINDING_SCHEMA_ID:
        raise ContractValidationError(
            "schema_id", "$.local_sf_qe.schema_id", "unsupported local SF-QE binding"
        )
    if result["model_id"] != SF_QE_MODEL_ID:
        raise ContractValidationError(
            "model_id", "$.local_sf_qe.model_id", "pilot SF-QE model is not approved"
        )
    if result["report_transform_id"] != SF_QE_REPORT_TRANSFORM_ID:
        raise ContractValidationError(
            "report_transform_id",
            "$.local_sf_qe.report_transform_id",
            "pilot SF-QE report transform is not approved",
        )
    if result["package_name"] != "unbabel-comet":
        raise ContractValidationError(
            "package_name",
            "$.local_sf_qe.package_name",
            "pilot SF-QE package is not approved",
        )
    if result["device"] != "cpu":
        raise ContractValidationError(
            "device",
            "$.local_sf_qe.device",
            "pilot SF-QE device must be CPU",
        )
    for field in ("checkpoint_sha256", "packet_set_sha256", "score_set_sha256"):
        result[field] = _require_sha256(row[field], path=f"$.local_sf_qe.{field}")
    for field in ("batch_size", "selected_job_count"):
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ContractValidationError(
                "integer", f"$.local_sf_qe.{field}", "expected a positive integer"
            )
        result[field] = value
    if result["batch_size"] > 512:
        raise ContractValidationError(
            "range", "$.local_sf_qe.batch_size", "batch size exceeds 512"
        )
    return result


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_sha256(text: str) -> str:
    if not isinstance(text, str):
        raise ContractValidationError("type", "$", "SF-QE text must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_sha256(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ContractValidationError("sha256", path, "expected a SHA-256 digest")
    lowered = value.lower()
    if any(char not in "0123456789abcdef" for char in lowered):
        raise ContractValidationError("sha256", path, "expected a SHA-256 digest")
    return lowered
