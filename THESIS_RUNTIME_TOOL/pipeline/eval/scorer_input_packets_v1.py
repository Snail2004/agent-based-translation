from __future__ import annotations

import hashlib
from typing import Any, Mapping

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    assert_no_forbidden_runtime_data,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.offline_orchestrator_v1 import EvaluationJobV1, EvaluationPlanV1


__all__ = [
    "SCORER_INPUT_PACKET_SCHEMA_ID",
    "SCORER_INPUT_PACKET_SCHEMA_VERSION",
    "build_scorer_input_packet",
    "seal_scorer_input_packet",
    "validate_scorer_input_packet",
    "validate_scorer_input_packet_binding",
]


SCORER_INPUT_PACKET_SCHEMA_ID = "EvaluationScorerInputPacketV1"
SCORER_INPUT_PACKET_SCHEMA_VERSION = "1.0.0"
PACKET_SELF_HASH_PATH = ("integrity", "packet_sha256")

SCORER_INPUT_PACKET_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("source", "blocks"),
            ("candidates",),
            ("candidates", "*", "blocks"),
        }
    ),
)

_METHOD_STAGE = {
    "sf_qe": "quality_estimation",
    "sf_bt": "back_translation",
    "pj": "pairwise_judgment",
}
_BLOCK_ROLES = frozenset({"preceding", "active", "following"})
_CANDIDATE_STATUSES = frozenset(
    {
        "translated",
        "preserved",
        "excluded",
        "review_held",
        "missing",
        "failed",
        "missing_row",
    }
)


def build_scorer_input_packet(
    common_input: CommonEvaluationInputV1,
    plan: EvaluationPlanV1,
    job_id: str,
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    """Build one sealed model-facing packet without exposing arm identity."""

    job = _find_job(plan, job_id)
    if job.status != "ready":
        raise ContractValidationError(
            "job_not_ready", "$.job_id", "blocked jobs cannot produce scorer input"
        )
    try:
        stage = _METHOD_STAGE[job.method_id]
    except KeyError as exc:
        raise ContractValidationError(
            "unsupported_method",
            "$.job_id",
            f"no scorer input view is defined for method {job.method_id!r}",
        ) from exc

    unit = next((row for row in plan.units if row.unit_id == job.unit_id), None)
    if unit is None:
        raise ContractValidationError(
            "unit_reference", "$.job_id", "job references an unknown plan unit"
        )

    block_index = {row.block_id: row for row in common_input.blocks}
    context_ids = tuple(unit.context_block_ids)
    if unit.block_id not in context_ids:
        raise ContractValidationError(
            "active_context", "$.job_id", "unit context omits its active block"
        )
    try:
        context_blocks = tuple(block_index[block_id] for block_id in context_ids)
    except KeyError as exc:
        raise ContractValidationError(
            "block_reference", "$.job_id", "unit context references a foreign block"
        ) from exc
    if any(row.chapter_id != unit.chapter_id for row in context_blocks):
        raise ContractValidationError(
            "cross_chapter_context",
            "$.job_id",
            "scorer context cannot cross a chapter boundary",
        )
    if tuple(row.order_index for row in context_blocks) != tuple(
        sorted(row.order_index for row in context_blocks)
    ):
        raise ContractValidationError(
            "context_order", "$.job_id", "context must preserve canonical source order"
        )

    arm_index = {row.arm_id: row for row in common_input.arms}
    translation_index = {
        (row.arm_id, row.block_id): row for row in common_input.translations
    }
    try:
        presented_arms = tuple(arm_index[arm_id] for arm_id in job.presentation_arm_ids)
    except KeyError as exc:
        raise ContractValidationError(
            "arm_reference", "$.job_id", "job references a foreign arm"
        ) from exc
    language_pairs = {
        (row.source_language, row.target_language) for row in presented_arms
    }
    if len(language_pairs) != 1:
        raise ContractValidationError(
            "language_pair",
            "$.job_id",
            "all presented candidates must share one language pair",
        )
    source_language, target_language = next(iter(language_pairs))

    visible_blocks = (
        (block_index[unit.block_id],)
        if job.method_id == "sf_qe"
        else context_blocks
    )
    source = None
    if job.method_id != "sf_bt":
        source = {
            "blocks": [
                _source_block_view(
                    row,
                    active_block_id=unit.block_id,
                    active_order_index=unit.order_index,
                )
                for row in visible_blocks
            ]
        }

    candidates: list[dict[str, Any]] = []
    for slot_index, arm in enumerate(presented_arms, start=1):
        rows: list[dict[str, Any]] = []
        for block in visible_blocks:
            translation = translation_index.get((arm.arm_id, block.block_id))
            status = "missing_row" if translation is None else translation.status
            text = None if translation is None else translation.target_text
            rows.append(
                _candidate_block_view(
                    block,
                    active_block_id=unit.block_id,
                    active_order_index=unit.order_index,
                    status=status,
                    text=text,
                )
            )
        candidates.append({"slot_id": f"candidate_{slot_index}", "blocks": rows})

    packet_id = "packet-" + _digest(plan.plan_sha256, job.job_id, stage)[:24]
    sealed = seal_scorer_input_packet(
        {
            "schema_id": SCORER_INPUT_PACKET_SCHEMA_ID,
            "schema_version": SCORER_INPUT_PACKET_SCHEMA_VERSION,
            "packet_id": packet_id,
            "created_at": created_at,
            "producer": {
                "workstream": "evaluation",
                "component": "scorer_input_packets_v1",
                "component_version": "1.0.0",
                "code_commit": producer_code_commit,
            },
            "binding": {
                "plan_id": plan.plan_id,
                "plan_sha256": plan.plan_sha256,
                "config_sha256": plan.config_sha256,
                "input_set_sha256": plan.input_set_sha256,
                "job_id": job.job_id,
                "method_id": job.method_id,
                "method_version": job.method_version,
                "unit_id": job.unit_id,
            },
            "languages": {
                "source_language": source_language,
                "target_language": target_language,
            },
            "stage": stage,
            "source": source,
            "candidates": candidates,
            "integrity": {"packet_sha256": "0" * 64},
        }
    )
    return validate_scorer_input_packet(sealed)


def seal_scorer_input_packet(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=SCORER_INPUT_PACKET_POLICY,
        hash_path=PACKET_SELF_HASH_PATH,
    )


def validate_scorer_input_packet(payload: Mapping[str, Any]) -> dict[str, Any]:
    assert_no_forbidden_runtime_data(payload)
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "packet_id",
            "created_at",
            "producer",
            "binding",
            "languages",
            "stage",
            "source",
            "candidates",
            "integrity",
        },
        path="$",
    )
    binding = _validate_binding(root["binding"])
    stage = require_enum(root["stage"], set(_METHOD_STAGE.values()), path="$.stage")
    source = _validate_source(root["source"])
    candidates = _validate_candidates(root["candidates"])
    integrity = _validate_integrity(root["integrity"])
    normalized: dict[str, Any] = {
        "schema_id": require_enum(
            root["schema_id"], {SCORER_INPUT_PACKET_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {SCORER_INPUT_PACKET_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "packet_id": require_string(root["packet_id"], path="$.packet_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": binding,
        "languages": _validate_languages(root["languages"]),
        "stage": stage,
        "source": source,
        "candidates": candidates,
        "integrity": integrity,
    }
    _validate_method_shape(normalized)
    if not verify_payload_hash(
        normalized,
        policy=SCORER_INPUT_PACKET_POLICY,
        hash_path=PACKET_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "packet_hash",
            "$.integrity.packet_sha256",
            "scorer packet self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=SCORER_INPUT_PACKET_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical scorer input packet must remain an object")
    return canonical


def validate_scorer_input_packet_binding(
    payload: Mapping[str, Any],
    common_input: CommonEvaluationInputV1,
    plan: EvaluationPlanV1,
) -> dict[str, Any]:
    """Validate a packet and prove every visible byte came from its bound job."""

    validated = validate_scorer_input_packet(payload)
    expected = build_scorer_input_packet(
        common_input,
        plan,
        validated["binding"]["job_id"],
        created_at=validated["created_at"],
        producer_code_commit=validated["producer"]["code_commit"],
    )
    if validated != expected:
        raise ContractValidationError(
            "packet_binding",
            "$",
            "scorer packet does not match its bound plan job and common input",
        )
    return validated


def _validate_binding(value: Any) -> dict[str, str]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "plan_id",
            "plan_sha256",
            "config_sha256",
            "input_set_sha256",
            "job_id",
            "method_id",
            "method_version",
            "unit_id",
        },
        path=path,
    )
    return {
        "plan_id": require_string(row["plan_id"], path=f"{path}.plan_id"),
        "plan_sha256": require_sha256(
            row["plan_sha256"], path=f"{path}.plan_sha256"
        ),
        "config_sha256": require_sha256(
            row["config_sha256"], path=f"{path}.config_sha256"
        ),
        "input_set_sha256": require_sha256(
            row["input_set_sha256"], path=f"{path}.input_set_sha256"
        ),
        "job_id": require_string(row["job_id"], path=f"{path}.job_id"),
        "method_id": require_enum(
            row["method_id"], set(_METHOD_STAGE), path=f"{path}.method_id"
        ),
        "method_version": require_string(
            row["method_version"], path=f"{path}.method_version"
        ),
        "unit_id": require_string(row["unit_id"], path=f"{path}.unit_id"),
    }


def _validate_languages(value: Any) -> dict[str, str]:
    path = "$.languages"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"source_language", "target_language"}, path=path
    )
    source = require_string(row["source_language"], path=f"{path}.source_language")
    target = require_string(row["target_language"], path=f"{path}.target_language")
    if source == target:
        raise ContractValidationError(
            "language_pair", path, "source and target languages must differ"
        )
    return {"source_language": source, "target_language": target}


def _validate_source(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    path = "$.source"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"blocks"}, path=path)
    return {"blocks": _validate_blocks(row["blocks"], path=f"{path}.blocks", source=True)}


def _validate_candidates(value: Any) -> list[dict[str, Any]]:
    path = "$.candidates"
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "at least one candidate is required")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw, path=row_path)
        require_exact_keys(row, required={"slot_id", "blocks"}, path=row_path)
        result.append(
            {
                "slot_id": require_string(row["slot_id"], path=f"{row_path}.slot_id"),
                "blocks": _validate_blocks(
                    row["blocks"], path=f"{row_path}.blocks", source=False
                ),
            }
        )
    require_unique([row["slot_id"] for row in result], path=f"{path}.slot_id")
    expected = [f"candidate_{index}" for index in range(1, len(result) + 1)]
    if [row["slot_id"] for row in result] != expected:
        raise ContractValidationError(
            "candidate_slots", path, "candidate slots must be contiguous and ordered"
        )
    return result


def _validate_blocks(value: Any, *, path: str, source: bool) -> list[dict[str, Any]]:
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "at least one block is required")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw, path=row_path)
        require_exact_keys(
            row,
            required={"block_id", "role", "block_type", "status", "text"},
            path=row_path,
        )
        status = require_enum(
            row["status"], {"source"} if source else _CANDIDATE_STATUSES,
            path=f"{row_path}.status",
        )
        text = require_nullable_string(
            row["text"], path=f"{row_path}.text", allow_empty=True
        )
        if source and text is None:
            raise ContractValidationError(
                "source_text", f"{row_path}.text", "source text cannot be null"
            )
        if not source:
            has_text = status in {"translated", "preserved"}
            if has_text != (text is not None):
                raise ContractValidationError(
                    "candidate_text_status",
                    row_path,
                    "only translated or preserved candidate blocks may carry text",
                )
        result.append(
            {
                "block_id": require_string(row["block_id"], path=f"{row_path}.block_id"),
                "role": require_enum(row["role"], _BLOCK_ROLES, path=f"{row_path}.role"),
                "block_type": require_string(
                    row["block_type"], path=f"{row_path}.block_type"
                ),
                "status": status,
                "text": text,
            }
        )
    require_unique([row["block_id"] for row in result], path=f"{path}.block_id")
    if sum(row["role"] == "active" for row in result) != 1:
        raise ContractValidationError(
            "active_block", path, "block sequence must contain exactly one active row"
        )
    roles = [row["role"] for row in result]
    active_index = roles.index("active")
    if any(role != "preceding" for role in roles[:active_index]) or any(
        role != "following" for role in roles[active_index + 1 :]
    ):
        raise ContractValidationError(
            "block_role_order", path, "block roles do not match sequence order"
        )
    return result


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"packet_sha256"}, path=path)
    return {
        "packet_sha256": require_sha256(
            row["packet_sha256"], path=f"{path}.packet_sha256"
        )
    }


def _validate_method_shape(packet: Mapping[str, Any]) -> None:
    method_id = packet["binding"]["method_id"]
    expected_stage = _METHOD_STAGE[method_id]
    if packet["stage"] != expected_stage:
        raise ContractValidationError(
            "method_stage", "$.stage", "stage does not match the declared method"
        )
    source = packet["source"]
    candidates = packet["candidates"]
    expected_count = 2 if method_id == "pj" else 1
    if len(candidates) != expected_count:
        raise ContractValidationError(
            "candidate_count",
            "$.candidates",
            f"{method_id} requires exactly {expected_count} candidate(s)",
        )
    if method_id == "sf_bt":
        if source is not None:
            raise ContractValidationError(
                "source_leak",
                "$.source",
                "back-translation input must not contain source text",
            )
    elif source is None:
        raise ContractValidationError(
            "source_required", "$.source", f"{method_id} requires source text"
        )

    if method_id == "sf_qe":
        if len(source["blocks"]) != 1 or len(candidates[0]["blocks"]) != 1:
            raise ContractValidationError(
                "qe_context", "$", "SF-QE accepts active source and target blocks only"
            )
    elif method_id == "pj":
        source_ids = [row["block_id"] for row in source["blocks"]]
        for index, candidate in enumerate(candidates):
            if [row["block_id"] for row in candidate["blocks"]] != source_ids:
                raise ContractValidationError(
                    "context_alignment",
                    f"$.candidates[{index}].blocks",
                    "PJ source and candidate context must align by block ID",
                )

    for index, candidate in enumerate(candidates):
        active = next(row for row in candidate["blocks"] if row["role"] == "active")
        if active["status"] != "translated":
            raise ContractValidationError(
                "active_candidate_status",
                f"$.candidates[{index}].blocks",
                "active scorer candidates must be translated",
            )


def _source_block_view(
    block: Any, *, active_block_id: str, active_order_index: int
) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "role": _block_role(
            block.block_id,
            active_block_id,
            block.order_index,
            active_order_index,
        ),
        "block_type": block.block_type,
        "status": "source",
        "text": block.source_text,
    }


def _candidate_block_view(
    block: Any,
    *,
    active_block_id: str,
    active_order_index: int,
    status: str,
    text: str | None,
) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "role": _block_role(
            block.block_id,
            active_block_id,
            block.order_index,
            active_order_index,
        ),
        "block_type": block.block_type,
        "status": status,
        "text": text,
    }


def _block_role(
    block_id: str,
    active_block_id: str,
    order_index: int,
    active_order_index: int,
) -> str:
    if block_id == active_block_id:
        return "active"
    return "preceding" if order_index < active_order_index else "following"


def _find_job(plan: EvaluationPlanV1, job_id: str) -> EvaluationJobV1:
    matches = [row for row in plan.jobs if row.job_id == job_id]
    if len(matches) != 1:
        raise ContractValidationError(
            "job_reference", "$.job_id", "job ID must resolve exactly once in the plan"
        )
    return matches[0]


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
