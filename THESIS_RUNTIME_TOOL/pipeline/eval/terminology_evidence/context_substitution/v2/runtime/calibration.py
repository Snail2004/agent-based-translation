from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_number,
    require_sha256,
    require_string,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.calibration_artifact import (
    load_calibration_artifact,
    validate_calibration_artifact,
)


DEVELOPMENT_POLICY_STATUS = "DEMO_HEURISTIC_REQUIRES_CALIBRATION"
FROZEN_POLICY_STATUS = "FROZEN_VALIDATION_POLICY"
EVALUATION_MODES = frozenset({"DEVELOPMENT", "FROZEN_TEST_SET"})


@dataclass(frozen=True)
class ContextThresholdPolicy:
    policy_version: str
    policy_status: str
    supported_min_c: float
    unsupported_below_c: float
    supported_min_pass: int
    supported_max_minor: int
    unsupported_min_fail: int
    second_judge_thresholds: tuple[float, ...]
    second_judge_tolerance: float
    pairwise_close_margin: float
    calibration_artifact_ref: str | None
    calibration_artifact_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "policy_status": self.policy_status,
            "supported_min_c": self.supported_min_c,
            "unsupported_below_c": self.unsupported_below_c,
            "supported_min_pass": self.supported_min_pass,
            "supported_max_minor": self.supported_max_minor,
            "unsupported_min_fail": self.unsupported_min_fail,
            "second_judge_thresholds": list(self.second_judge_thresholds),
            "second_judge_tolerance": self.second_judge_tolerance,
            "pairwise_close_margin": self.pairwise_close_margin,
            "calibration_artifact_ref": self.calibration_artifact_ref,
            "calibration_artifact_sha256": self.calibration_artifact_sha256,
        }


DEVELOPMENT_HEURISTIC_POLICY = ContextThresholdPolicy(
    policy_version="d2l_context_status_development_heuristic_v2_1",
    policy_status=DEVELOPMENT_POLICY_STATUS,
    supported_min_c=0.80,
    unsupported_below_c=0.60,
    supported_min_pass=4,
    supported_max_minor=1,
    unsupported_min_fail=2,
    second_judge_thresholds=(0.60, 0.70, 0.80),
    second_judge_tolerance=0.04,
    pairwise_close_margin=0.067,
    calibration_artifact_ref=None,
    calibration_artifact_sha256=None,
)


def frozen_validation_policy(
    *,
    calibration_artifact: Path | Mapping[str, Any],
    expected_physical_sha256: str | None = None,
) -> ContextThresholdPolicy:
    if isinstance(calibration_artifact, Mapping):
        artifact = validate_calibration_artifact(calibration_artifact)
    else:
        artifact = load_calibration_artifact(
            Path(calibration_artifact),
            expected_physical_sha256=expected_physical_sha256,
        )
    selected = artifact["selected_policy"]
    artifact_hash = artifact["integrity"]["artifact_sha256"]
    return validate_threshold_policy(
        {
            "policy_version": selected["policy_version"],
            "policy_status": FROZEN_POLICY_STATUS,
            "supported_min_c": selected["supported_min_c"],
            "unsupported_below_c": selected["unsupported_below_c"],
            "supported_min_pass": selected["supported_min_pass"],
            "supported_max_minor": selected["supported_max_minor"],
            "unsupported_min_fail": selected["unsupported_min_fail"],
            "second_judge_thresholds": list(selected["second_judge_thresholds"]),
            "second_judge_tolerance": selected["second_judge_tolerance"],
            "pairwise_close_margin": selected["pairwise_close_margin"],
            "calibration_artifact_ref": (
                f"artifact://cst-calibration/{artifact_hash}/calibration.json"
            ),
            "calibration_artifact_sha256": artifact_hash,
        }
    )


def validate_evaluation_mode(
    mode: str, policy: ContextThresholdPolicy
) -> str:
    normalized = require_enum(mode, EVALUATION_MODES, path="$.evaluation_mode")
    if (
        normalized == "FROZEN_TEST_SET"
        and policy.policy_status != FROZEN_POLICY_STATUS
    ):
        raise ContractValidationError(
            "uncalibrated_test_set",
            "$.threshold_policy",
            "frozen test-set execution requires a sealed calibration policy",
        )
    return normalized


def validate_threshold_policy(value: Any) -> ContextThresholdPolicy:
    path = "$.threshold_policy"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "policy_version",
            "policy_status",
            "supported_min_c",
            "unsupported_below_c",
            "supported_min_pass",
            "supported_max_minor",
            "unsupported_min_fail",
            "second_judge_thresholds",
            "second_judge_tolerance",
            "pairwise_close_margin",
            "calibration_artifact_ref",
            "calibration_artifact_sha256",
        },
        path=path,
    )
    status = require_enum(
        row["policy_status"],
        {DEVELOPMENT_POLICY_STATUS, FROZEN_POLICY_STATUS},
        path=f"{path}.policy_status",
    )
    supported_min_c = _unit_interval(
        row["supported_min_c"], path=f"{path}.supported_min_c"
    )
    unsupported_below_c = _unit_interval(
        row["unsupported_below_c"], path=f"{path}.unsupported_below_c"
    )
    if unsupported_below_c >= supported_min_c:
        raise ContractValidationError(
            "threshold_order",
            path,
            "unsupported threshold must be below the supported threshold",
        )
    thresholds = tuple(
        _unit_interval(child, path=f"{path}.second_judge_thresholds[{index}]")
        for index, child in enumerate(
            require_list(
                row["second_judge_thresholds"],
                path=f"{path}.second_judge_thresholds",
            )
        )
    )
    if not thresholds or tuple(sorted(set(thresholds))) != thresholds:
        raise ContractValidationError(
            "threshold_order",
            f"{path}.second_judge_thresholds",
            "thresholds must be unique and sorted",
        )
    calibration_ref = require_nullable_string(
        row["calibration_artifact_ref"],
        path=f"{path}.calibration_artifact_ref",
        maximum=4_000,
    )
    calibration_sha = require_nullable_string(
        row["calibration_artifact_sha256"],
        path=f"{path}.calibration_artifact_sha256",
    )
    if calibration_sha is not None:
        calibration_sha = require_sha256(
            calibration_sha, path=f"{path}.calibration_artifact_sha256"
        )
    if status == FROZEN_POLICY_STATUS and (
        calibration_sha is None or calibration_ref is None
    ):
        raise ContractValidationError(
            "calibration_binding", path, "frozen policy requires an artifact ref/hash"
        )
    if status == FROZEN_POLICY_STATUS:
        expected_ref = f"artifact://cst-calibration/{calibration_sha}/calibration.json"
        if calibration_ref != expected_ref:
            raise ContractValidationError(
                "calibration_binding",
                f"{path}.calibration_artifact_ref",
                "calibration reference does not bind the artifact hash",
            )
    if status == DEVELOPMENT_POLICY_STATUS and (
        calibration_sha is not None or calibration_ref is not None
    ):
        raise ContractValidationError(
            "calibration_binding", path, "development policy cannot claim calibration"
        )
    return ContextThresholdPolicy(
        policy_version=require_string(
            row["policy_version"], path=f"{path}.policy_version", maximum=500
        ),
        policy_status=status,
        supported_min_c=supported_min_c,
        unsupported_below_c=unsupported_below_c,
        supported_min_pass=require_int(
            row["supported_min_pass"],
            path=f"{path}.supported_min_pass",
            minimum=1,
        ),
        supported_max_minor=require_int(
            row["supported_max_minor"],
            path=f"{path}.supported_max_minor",
            minimum=0,
        ),
        unsupported_min_fail=require_int(
            row["unsupported_min_fail"],
            path=f"{path}.unsupported_min_fail",
            minimum=1,
        ),
        second_judge_thresholds=thresholds,
        second_judge_tolerance=_unit_interval(
            row["second_judge_tolerance"],
            path=f"{path}.second_judge_tolerance",
        ),
        pairwise_close_margin=_unit_interval(
            row["pairwise_close_margin"],
            path=f"{path}.pairwise_close_margin",
        ),
        calibration_artifact_ref=calibration_ref,
        calibration_artifact_sha256=calibration_sha,
    )


def _unit_interval(value: Any, *, path: str) -> float:
    result = float(require_number(value, path=path, minimum=0))
    if result > 1:
        raise ContractValidationError("range", path, "must be <= 1")
    return result

