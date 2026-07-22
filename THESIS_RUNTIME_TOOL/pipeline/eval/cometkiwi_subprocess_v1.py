from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import ContractValidationError


__all__ = [
    "COMETKIWI_BATCH_REQUEST_SCHEMA_ID",
    "COMETKIWI_BATCH_RESPONSE_SCHEMA_ID",
    "COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID",
    "CometKiwiSubprocessPredictorV1",
    "validate_cometkiwi_batch_request_v1",
    "validate_cometkiwi_batch_response_v1",
    "validate_cometkiwi_runtime_description_v1",
]


COMETKIWI_BATCH_REQUEST_SCHEMA_ID = "CometKiwiBatchRequestV1"
COMETKIWI_BATCH_RESPONSE_SCHEMA_ID = "CometKiwiBatchResponseV1"
COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID = "CometKiwiRuntimeDescriptionV1"
_WORKER_MODULE = "pipeline.eval.cometkiwi_worker_v1"
_MAX_BATCH_ROWS = 10_000


class CometKiwiSubprocessPredictorV1:
    """Run a pinned local COMET checkpoint in an explicit Python environment."""

    def __init__(
        self,
        *,
        python_executable: Path,
        checkpoint_path: Path,
        timeout_seconds: int = 1_800,
        max_rows_per_worker: int | None = None,
    ) -> None:
        self._python = _require_file(python_executable, path="$.python_executable")
        self._checkpoint = _require_file(
            checkpoint_path,
            path="$.checkpoint_path",
            preserve_symlink=True,
        )
        self._checkpoint_sha256 = _sha256_file(self._checkpoint)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
            raise ContractValidationError(
                "type", "$.timeout_seconds", "timeout must be an integer"
            )
        if timeout_seconds < 1 or timeout_seconds > 7_200:
            raise ContractValidationError(
                "range", "$.timeout_seconds", "timeout must be within 1..7200 seconds"
            )
        self._timeout_seconds = timeout_seconds
        if max_rows_per_worker is not None:
            if isinstance(max_rows_per_worker, bool) or not isinstance(
                max_rows_per_worker, int
            ):
                raise ContractValidationError(
                    "type",
                    "$.max_rows_per_worker",
                    "worker row cap must be an integer or null",
                )
            if max_rows_per_worker < 1 or max_rows_per_worker > _MAX_BATCH_ROWS:
                raise ContractValidationError(
                    "range",
                    "$.max_rows_per_worker",
                    f"worker row cap must be within 1..{_MAX_BATCH_ROWS}",
                )
        self._max_rows_per_worker = max_rows_per_worker
        self._runtime_root = Path(__file__).resolve().parents[2]

    @property
    def checkpoint_sha256(self) -> str:
        return self._checkpoint_sha256

    def describe_runtime(self) -> dict[str, str]:
        completed = self._invoke(["--describe"], input_text=None)
        return validate_cometkiwi_runtime_description_v1(
            _load_json_object(completed.stdout, path="$.worker_stdout"),
            expected_checkpoint_sha256=self.checkpoint_sha256,
        )

    def __call__(
        self, rows: Sequence[Mapping[str, str]], batch_size: int
    ) -> Sequence[float]:
        request = validate_cometkiwi_batch_request_v1(
            {
                "schema_id": COMETKIWI_BATCH_REQUEST_SCHEMA_ID,
                "rows": copy.deepcopy(list(rows)),
            }
        )
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise ContractValidationError(
                "type", "$.batch_size", "batch size must be an integer"
            )
        if batch_size < 1 or batch_size > 512:
            raise ContractValidationError(
                "range", "$.batch_size", "batch size must be within 1..512"
            )
        rows_per_worker = self._max_rows_per_worker or len(request["rows"])
        scores: list[float] = []
        for start in range(0, len(request["rows"]), rows_per_worker):
            chunk = {
                "schema_id": COMETKIWI_BATCH_REQUEST_SCHEMA_ID,
                "rows": request["rows"][start : start + rows_per_worker],
            }
            completed = self._invoke(
                ["--batch-size", str(batch_size)],
                input_text=_canonical_json(chunk),
            )
            response = validate_cometkiwi_batch_response_v1(
                _load_json_object(completed.stdout, path="$.worker_stdout"),
                expected_count=len(chunk["rows"]),
            )
            scores.extend(response["scores"])
        return scores

    def _invoke(
        self, extra_args: Sequence[str], *, input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(self._python),
            "-m",
            _WORKER_MODULE,
            "--checkpoint",
            str(self._checkpoint),
            *extra_args,
        ]
        try:
            worker_env = os.environ.copy()
            worker_env.update(
                {
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "HF_DATASETS_OFFLINE": "1",
                    "WANDB_MODE": "disabled",
                }
            )
            completed = subprocess.run(
                command,
                cwd=self._runtime_root,
                env=worker_env,
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="strict",
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContractValidationError(
                "cometkiwi_timeout",
                "$.sf_qe_runtime",
                "local COMET worker exceeded its sealed timeout",
            ) from exc
        except OSError as exc:
            raise ContractValidationError(
                "cometkiwi_transport",
                "$.sf_qe_runtime",
                "local COMET worker could not be started",
            ) from exc
        if completed.returncode != 0:
            raise ContractValidationError(
                "cometkiwi_worker_failure",
                "$.sf_qe_runtime",
                f"local COMET worker exited with code {completed.returncode}",
            )
        return completed


def validate_cometkiwi_batch_request_v1(value: Any) -> dict[str, Any]:
    row = _require_mapping(value, path="$")
    _require_exact_keys(row, {"schema_id", "rows"}, path="$")
    if row["schema_id"] != COMETKIWI_BATCH_REQUEST_SCHEMA_ID:
        raise ContractValidationError(
            "schema_id", "$.schema_id", "unsupported COMET batch request schema"
        )
    raw_rows = _require_list(row["rows"], path="$.rows")
    if not raw_rows or len(raw_rows) > _MAX_BATCH_ROWS:
        raise ContractValidationError(
            "batch_size", "$.rows", f"COMET batch must contain 1..{_MAX_BATCH_ROWS} rows"
        )
    normalized: list[dict[str, str]] = []
    for index, raw in enumerate(raw_rows):
        path = f"$.rows[{index}]"
        item = _require_mapping(raw, path=path)
        _require_exact_keys(item, {"src", "mt"}, path=path)
        normalized.append(
            {
                "src": _require_nonempty_text(item["src"], path=f"{path}.src"),
                "mt": _require_nonempty_text(item["mt"], path=f"{path}.mt"),
            }
        )
    return {"schema_id": COMETKIWI_BATCH_REQUEST_SCHEMA_ID, "rows": normalized}


def validate_cometkiwi_batch_response_v1(
    value: Any, *, expected_count: int
) -> dict[str, Any]:
    row = _require_mapping(value, path="$")
    _require_exact_keys(row, {"schema_id", "scores"}, path="$")
    if row["schema_id"] != COMETKIWI_BATCH_RESPONSE_SCHEMA_ID:
        raise ContractValidationError(
            "schema_id", "$.schema_id", "unsupported COMET batch response schema"
        )
    scores = _require_list(row["scores"], path="$.scores")
    if len(scores) != expected_count:
        raise ContractValidationError(
            "result_count", "$.scores", "COMET score count differs from request count"
        )
    normalized: list[float] = []
    for index, value in enumerate(scores):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractValidationError(
                "score_type", f"$.scores[{index}]", "COMET score must be numeric"
            )
        score = float(value)
        if not math.isfinite(score) or score < 0 or score > 1:
            raise ContractValidationError(
                "score_range",
                f"$.scores[{index}]",
                "COMET score must be finite within [0, 1]",
            )
        normalized.append(score)
    return {"schema_id": COMETKIWI_BATCH_RESPONSE_SCHEMA_ID, "scores": normalized}


def validate_cometkiwi_runtime_description_v1(
    value: Any, *, expected_checkpoint_sha256: str
) -> dict[str, str]:
    row = _require_mapping(value, path="$")
    required = {
        "schema_id",
        "package_name",
        "package_version",
        "python_version",
        "device",
        "checkpoint_sha256",
    }
    _require_exact_keys(row, required, path="$")
    if row["schema_id"] != COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID:
        raise ContractValidationError(
            "schema_id", "$.schema_id", "unsupported COMET runtime description"
        )
    expected = _require_sha256(
        expected_checkpoint_sha256, path="$.expected_checkpoint_sha256"
    )
    observed = _require_sha256(
        row["checkpoint_sha256"], path="$.checkpoint_sha256"
    )
    if observed != expected:
        raise ContractValidationError(
            "checkpoint_hash",
            "$.checkpoint_sha256",
            "COMET worker loaded a different checkpoint",
        )
    package_name = _require_nonempty_text(
        row["package_name"], path="$.package_name"
    )
    if package_name != "unbabel-comet":
        raise ContractValidationError(
            "package_name", "$.package_name", "COMET worker package is not approved"
        )
    device = _require_nonempty_text(row["device"], path="$.device")
    if device != "cpu":
        raise ContractValidationError(
            "device", "$.device", "pilot COMET worker must be CPU-bound"
        )
    return {
        "schema_id": COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID,
        "package_name": package_name,
        "package_version": _require_nonempty_text(
            row["package_version"], path="$.package_version"
        ),
        "python_version": _require_nonempty_text(
            row["python_version"], path="$.python_version"
        ),
        "device": device,
        "checkpoint_sha256": observed,
    }


def _load_json_object(text: str, *, path: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError(
            "worker_json", path, "local COMET worker returned invalid JSON"
        ) from exc
    return _require_mapping(value, path=path)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_file(
    value: Path, *, path: str, preserve_symlink: bool = False
) -> Path:
    if not isinstance(value, Path):
        raise ContractValidationError("type", path, "expected a pathlib.Path")
    expanded = value.expanduser()
    normalized = (
        Path(os.path.abspath(expanded))
        if preserve_symlink
        else expanded.resolve()
    )
    if not normalized.is_file():
        raise ContractValidationError("missing_file", path, "required file is missing")
    return normalized


def _require_mapping(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError("type", path, "expected an object")
    return copy.deepcopy(dict(value))


def _require_list(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractValidationError("type", path, "expected an array")
    return copy.deepcopy(value)


def _require_exact_keys(value: Mapping[str, Any], required: set[str], *, path: str) -> None:
    keys = set(value)
    if keys != required:
        raise ContractValidationError(
            "closed_schema", path, "object fields differ from the closed contract"
        )


def _require_nonempty_text(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise ContractValidationError("type", path, "expected a string")
    if not value.strip():
        raise ContractValidationError("empty_string", path, "string may not be blank")
    return value


def _require_sha256(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ContractValidationError("sha256", path, "expected a SHA-256 hex digest")
    lowered = value.lower()
    if any(char not in "0123456789abcdef" for char in lowered):
        raise ContractValidationError("sha256", path, "expected a SHA-256 hex digest")
    return lowered


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
