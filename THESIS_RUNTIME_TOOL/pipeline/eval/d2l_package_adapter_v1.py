from __future__ import annotations

from typing import Any, Mapping

from pipeline.eval.common_input_v1 import (
    CommonArmV1,
    CommonEvaluationInputV1,
    CommonTranslationV1,
    project_d2l_source_snapshot,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.d2l_input_v1 import validate_d2l_evaluation_input


__all__ = ["project_d2l_evaluation_package"]


_STATUS_MAP = {
    "translated": "translated",
    "passthrough": "preserved",
    "missing": "missing",
    "failed": "failed",
}


def project_d2l_evaluation_package(
    payload: Mapping[str, Any],
) -> CommonEvaluationInputV1:
    """Project a sealed D2LEvaluationInputV1 into the common offline read model.

    This compatibility adapter emits no producer artifact and grants no new
    authority. It preserves the package's legacy D2L source binding, uses the
    producer's experiment ID as the concrete attempt identity, and binds the
    profile configuration to the exact runtime-profile artifact bytes.
    """

    validated = validate_d2l_evaluation_input(payload)
    source = project_d2l_source_snapshot(validated)
    identity = validated["identity"]
    profile = validated["runtime_profile"]

    artifacts = {row["artifact_id"]: row for row in validated["artifacts"]}
    profile_artifact = artifacts[profile["source_artifact_id"]]
    if profile_artifact["kind"] != "runtime_profile":
        raise ContractValidationError(
            "artifact_kind",
            "$.runtime_profile.source_artifact_id",
            "runtime profile must reference a runtime_profile artifact",
        )

    arm_rows = {row["arm_id"]: row for row in validated["arms"]}
    translation_rows = {
        (row["arm_id"], row["block_id"]): row
        for row in validated["translations"]
    }

    arms = tuple(
        CommonArmV1(
            artifact_id=arm["translation_artifact_id"],
            artifact_sha256=arm["translation_sha256"],
            logical_run_id=identity["logical_run_id"],
            attempt_run_id=identity["experiment_id"],
            arm_id=arm["arm_id"],
            profile_id=profile["profile_id"],
            profile_config_sha256=profile_artifact["sha256"],
            source_language=profile["source_language"],
            target_language=profile["target_language"],
        )
        for arm in sorted(validated["arms"], key=lambda row: row["arm_id"])
    )

    translations: list[CommonTranslationV1] = []
    for arm_id in sorted(arm_rows):
        for block in source.blocks:
            if block.admission == "exclude":
                translations.append(
                    CommonTranslationV1(
                        arm_id=arm_id,
                        block_id=block.block_id,
                        status="excluded",
                        target_text=None,
                        error_code=None,
                    )
                )
                continue

            row = translation_rows.get((arm_id, block.block_id))
            if row is None:
                # The public D2L validator should already reject this. Keep the
                # adapter fail-closed if its upstream contract ever drifts.
                raise ContractValidationError(
                    "translation_exact_cover",
                    "$.translations",
                    f"missing translation row for {arm_id}/{block.block_id}",
                )
            translations.append(
                CommonTranslationV1(
                    arm_id=arm_id,
                    block_id=block.block_id,
                    status=_STATUS_MAP[row["status"]],
                    target_text=row["target_text"],
                    error_code=row["error_code"],
                )
            )

    return CommonEvaluationInputV1(
        source_schema_id=source.source_schema_id,
        source_schema_version=source.source_schema_version,
        source_binding=source.source_binding,
        blocks=source.blocks,
        arms=arms,
        translations=tuple(translations),
    )
