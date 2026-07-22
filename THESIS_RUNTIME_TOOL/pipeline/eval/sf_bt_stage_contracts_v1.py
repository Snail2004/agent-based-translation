from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Mapping

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    assert_no_forbidden_runtime_data,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_rfc3339,
    require_sha256,
    require_string,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.offline_orchestrator_v1 import EvaluationPlanV1
from pipeline.eval.scorer_input_packets_v1 import (
    validate_scorer_input_packet,
    validate_scorer_input_packet_binding,
)


__all__ = [
    "SF_BACK_TRANSLATION_RESULT_SCHEMA_ID",
    "SF_BACK_TRANSLATION_RESULT_SCHEMA_VERSION",
    "SF_BT_REVERSE_CANDIDATE_ID",
    "SF_BT_REVERSE_PROMPT_SHA256",
    "SF_BT_SEMANTIC_PACKET_SCHEMA_ID",
    "SF_BT_SEMANTIC_PACKET_SCHEMA_VERSION",
    "build_sf_back_translation_result",
    "build_sf_bt_semantic_judge_packet",
    "seal_sf_back_translation_result",
    "seal_sf_bt_semantic_judge_packet",
    "validate_sf_back_translation_result",
    "validate_sf_back_translation_result_binding",
    "validate_sf_bt_semantic_judge_packet",
    "validate_sf_bt_semantic_judge_packet_binding",
]


SF_BACK_TRANSLATION_RESULT_SCHEMA_ID = "SFBackTranslationResultV1"
SF_BACK_TRANSLATION_RESULT_SCHEMA_VERSION = "1.0.0"
SF_BT_SEMANTIC_PACKET_SCHEMA_ID = "SFBTSemanticJudgeInputPacketV1"
SF_BT_SEMANTIC_PACKET_SCHEMA_VERSION = "1.0.0"

SF_BT_REVERSE_CANDIDATE_ID = "sf_bt_reverse_v3_1_candidate"
SF_BT_REVERSE_PROMPT_SHA256 = (
    "5965e61a247256c93395c915cd91bd5e089b5e3f1d7b29b8d330ca3d01144ffd"
)

_RESULT_SELF_HASH_PATH = ("integrity", "result_sha256")
_PACKET_SELF_HASH_PATH = ("integrity", "packet_sha256")
_RESULT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(),
)
_PACKET_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("passages",)}),
)
_PASSAGE_SLOT_IDS = frozenset({"passage_a", "passage_b"})
_CONTEXT_PROFILES = frozenset({"no_context", "bounded_neighbors"})


def build_sf_back_translation_result(
    stage1_packet: Mapping[str, Any],
    *,
    attempt_id: str,
    attempt_index: int,
    created_at: str,
    producer_code_commit: str,
    context_profile: str,
    rendered_prompt_sha256: str,
    model_profile: Mapping[str, Any],
    completion_status: str,
    finish_reason: str,
    raw_response_text: str,
) -> dict[str, Any]:
    packet = validate_scorer_input_packet(stage1_packet)
    _require_stage1_packet(packet)
    back_translation = _parse_back_translation_response(raw_response_text)
    raw_response_sha256 = hashlib.sha256(raw_response_text.encode("utf-8")).hexdigest()

    result_id = "sfbt-result-" + _digest(
        packet["integrity"]["packet_sha256"],
        attempt_id,
        raw_response_sha256,
    )[:24]
    sealed = seal_sf_back_translation_result(
        {
            "schema_id": SF_BACK_TRANSLATION_RESULT_SCHEMA_ID,
            "schema_version": SF_BACK_TRANSLATION_RESULT_SCHEMA_VERSION,
            "result_id": result_id,
            "created_at": created_at,
            "producer": {
                "workstream": "evaluation",
                "component": "sf_bt_stage_contracts_v1",
                "component_version": "1.0.0",
                "code_commit": producer_code_commit,
            },
            "binding": {
                "stage1_packet_id": packet["packet_id"],
                "stage1_packet_sha256": packet["integrity"]["packet_sha256"],
                **packet["binding"],
                "attempt_id": attempt_id,
                "attempt_index": attempt_index,
            },
            "prompt": {
                "candidate_id": SF_BT_REVERSE_CANDIDATE_ID,
                "prompt_sha256": SF_BT_REVERSE_PROMPT_SHA256,
                "context_profile": context_profile,
                "rendered_prompt_sha256": rendered_prompt_sha256,
            },
            "model_profile": dict(model_profile),
            "transport": {
                "completion_status": completion_status,
                "finish_reason": finish_reason,
                "raw_response_sha256": raw_response_sha256,
            },
            "output": {"back_translation": back_translation},
            "integrity": {"result_sha256": "0" * 64},
        }
    )
    return validate_sf_back_translation_result(sealed)


def seal_sf_back_translation_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=_RESULT_POLICY,
        hash_path=_RESULT_SELF_HASH_PATH,
    )


def validate_sf_back_translation_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    assert_no_forbidden_runtime_data(payload)
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "result_id",
            "created_at",
            "producer",
            "binding",
            "prompt",
            "model_profile",
            "transport",
            "output",
            "integrity",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {SF_BACK_TRANSLATION_RESULT_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {SF_BACK_TRANSLATION_RESULT_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "result_id": require_string(root["result_id"], path="$.result_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_result_binding(root["binding"]),
        "prompt": _validate_prompt_binding(root["prompt"]),
        "model_profile": _validate_model_profile(root["model_profile"]),
        "transport": _validate_transport(root["transport"]),
        "output": _validate_back_translation_output(root["output"]),
        "integrity": _validate_integrity(
            root["integrity"], field="result_sha256", path="$.integrity"
        ),
    }
    if not verify_payload_hash(
        normalized,
        policy=_RESULT_POLICY,
        hash_path=_RESULT_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "result_hash",
            "$.integrity.result_sha256",
            "back-translation result self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=_RESULT_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical back-translation result must remain an object")
    return canonical


def validate_sf_back_translation_result_binding(
    payload: Mapping[str, Any],
    stage1_packet: Mapping[str, Any],
    *,
    raw_response_text: str,
    context_profile: str,
    rendered_prompt_sha256: str,
) -> dict[str, Any]:
    validated = validate_sf_back_translation_result(payload)
    packet = validate_scorer_input_packet(stage1_packet)
    _require_stage1_packet(packet)

    expected_binding = {
        "stage1_packet_id": packet["packet_id"],
        "stage1_packet_sha256": packet["integrity"]["packet_sha256"],
        **packet["binding"],
        "attempt_id": validated["binding"]["attempt_id"],
        "attempt_index": validated["binding"]["attempt_index"],
    }
    if validated["binding"] != expected_binding:
        raise ContractValidationError(
            "stage1_binding",
            "$.binding",
            "result does not bind to the supplied stage-1 packet",
        )
    expected_prompt = {
        "candidate_id": SF_BT_REVERSE_CANDIDATE_ID,
        "prompt_sha256": SF_BT_REVERSE_PROMPT_SHA256,
        "context_profile": require_enum(
            context_profile, _CONTEXT_PROFILES, path="$.context_profile"
        ),
        "rendered_prompt_sha256": require_sha256(
            rendered_prompt_sha256, path="$.rendered_prompt_sha256"
        ),
    }
    if validated["prompt"] != expected_prompt:
        raise ContractValidationError(
            "stage1_prompt_binding",
            "$.prompt",
            "result does not bind to the supplied rendered stage-1 prompt",
        )
    parsed = _parse_back_translation_response(raw_response_text)
    if parsed != validated["output"]["back_translation"]:
        raise ContractValidationError(
            "raw_response_binding",
            "$.output.back_translation",
            "parsed raw response does not match the recorded output",
        )
    raw_digest = hashlib.sha256(raw_response_text.encode("utf-8")).hexdigest()
    if raw_digest != validated["transport"]["raw_response_sha256"]:
        raise ContractValidationError(
            "raw_response_binding",
            "$.transport.raw_response_sha256",
            "raw response hash does not match the supplied bytes",
        )
    expected_result_id = "sfbt-result-" + _digest(
        packet["integrity"]["packet_sha256"],
        validated["binding"]["attempt_id"],
        raw_digest,
    )[:24]
    if validated["result_id"] != expected_result_id:
        raise ContractValidationError(
            "result_id_binding",
            "$.result_id",
            "result ID does not match packet, attempt, and raw response",
        )
    return validated


def build_sf_bt_semantic_judge_packet(
    common_input: CommonEvaluationInputV1,
    plan: EvaluationPlanV1,
    stage1_packet: Mapping[str, Any],
    stage1_result: Mapping[str, Any],
    *,
    stage1_raw_response_text: str,
    stage1_context_profile: str,
    stage1_rendered_prompt_sha256: str,
    presentation_id: str,
    source_first: bool,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    packet = validate_scorer_input_packet_binding(stage1_packet, common_input, plan)
    result = validate_sf_back_translation_result_binding(
        stage1_result,
        packet,
        raw_response_text=stage1_raw_response_text,
        context_profile=stage1_context_profile,
        rendered_prompt_sha256=stage1_rendered_prompt_sha256,
    )
    _require_stage1_packet(packet)
    if result["binding"]["plan_sha256"] != plan.plan_sha256:
        raise ContractValidationError(
            "plan_binding", "$.binding.plan_sha256", "stage-1 result references another plan"
        )

    unit = next(
        (row for row in plan.units if row.unit_id == packet["binding"]["unit_id"]),
        None,
    )
    if unit is None:
        raise ContractValidationError(
            "unit_reference", "$.binding.unit_id", "stage-1 packet references no plan unit"
        )
    source_block = next(
        (row for row in common_input.blocks if row.block_id == unit.block_id),
        None,
    )
    if source_block is None:
        raise ContractValidationError(
            "block_reference", "$.binding.unit_id", "plan unit references no source block"
        )
    source_text = unicodedata.normalize("NFC", source_block.source_text)
    back_translation = unicodedata.normalize(
        "NFC", result["output"]["back_translation"]
    )
    source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    back_translation_sha256 = hashlib.sha256(
        back_translation.encode("utf-8")
    ).hexdigest()

    source_slot_id = "passage_a" if source_first else "passage_b"
    back_translation_slot_id = "passage_b" if source_first else "passage_a"
    passage_text = {
        source_slot_id: source_text,
        back_translation_slot_id: back_translation,
    }
    passages = [
        {
            "slot_id": slot_id,
            "text": passage_text[slot_id],
            "text_sha256": hashlib.sha256(
                passage_text[slot_id].encode("utf-8")
            ).hexdigest(),
        }
        for slot_id in ("passage_a", "passage_b")
    ]
    packet_id = "sfbt-judge-packet-" + _digest(
        result["integrity"]["result_sha256"],
        presentation_id,
        source_slot_id,
    )[:24]
    sealed = seal_sf_bt_semantic_judge_packet(
        {
            "schema_id": SF_BT_SEMANTIC_PACKET_SCHEMA_ID,
            "schema_version": SF_BT_SEMANTIC_PACKET_SCHEMA_VERSION,
            "packet_id": packet_id,
            "created_at": created_at,
            "producer": {
                "workstream": "evaluation",
                "component": "sf_bt_stage_contracts_v1",
                "component_version": "1.0.0",
                "code_commit": producer_code_commit,
            },
            "binding": {
                "stage1_result_id": result["result_id"],
                "stage1_result_sha256": result["integrity"]["result_sha256"],
                "stage1_packet_id": packet["packet_id"],
                "stage1_packet_sha256": packet["integrity"]["packet_sha256"],
                **packet["binding"],
                "source_block_id": source_block.block_id,
                "source_text_sha256": source_sha256,
                "back_translation_sha256": back_translation_sha256,
                "presentation_id": presentation_id,
                "source_slot_id": source_slot_id,
                "back_translation_slot_id": back_translation_slot_id,
            },
            "language": "en",
            "stage": "semantic_comparison",
            "passages": passages,
            "integrity": {"packet_sha256": "0" * 64},
        }
    )
    return validate_sf_bt_semantic_judge_packet(sealed)


def seal_sf_bt_semantic_judge_packet(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=_PACKET_POLICY,
        hash_path=_PACKET_SELF_HASH_PATH,
    )


def validate_sf_bt_semantic_judge_packet(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
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
            "language",
            "stage",
            "passages",
            "integrity",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {SF_BT_SEMANTIC_PACKET_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {SF_BT_SEMANTIC_PACKET_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "packet_id": require_string(root["packet_id"], path="$.packet_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_semantic_binding(root["binding"]),
        "language": require_enum(root["language"], {"en"}, path="$.language"),
        "stage": require_enum(
            root["stage"], {"semantic_comparison"}, path="$.stage"
        ),
        "passages": _validate_passages(root["passages"]),
        "integrity": _validate_integrity(
            root["integrity"], field="packet_sha256", path="$.integrity"
        ),
    }
    _validate_semantic_shape(normalized)
    if not verify_payload_hash(
        normalized,
        policy=_PACKET_POLICY,
        hash_path=_PACKET_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "packet_hash",
            "$.integrity.packet_sha256",
            "semantic-judge packet self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=_PACKET_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical semantic-judge packet must remain an object")
    return canonical


def validate_sf_bt_semantic_judge_packet_binding(
    payload: Mapping[str, Any],
    common_input: CommonEvaluationInputV1,
    plan: EvaluationPlanV1,
    stage1_packet: Mapping[str, Any],
    stage1_result: Mapping[str, Any],
    *,
    stage1_raw_response_text: str,
    stage1_context_profile: str,
    stage1_rendered_prompt_sha256: str,
) -> dict[str, Any]:
    validated = validate_sf_bt_semantic_judge_packet(payload)
    expected = build_sf_bt_semantic_judge_packet(
        common_input,
        plan,
        stage1_packet,
        stage1_result,
        stage1_raw_response_text=stage1_raw_response_text,
        stage1_context_profile=stage1_context_profile,
        stage1_rendered_prompt_sha256=stage1_rendered_prompt_sha256,
        presentation_id=validated["binding"]["presentation_id"],
        source_first=validated["binding"]["source_slot_id"] == "passage_a",
        created_at=validated["created_at"],
        producer_code_commit=validated["producer"]["code_commit"],
    )
    if validated != expected:
        raise ContractValidationError(
            "semantic_packet_binding",
            "$",
            "semantic-judge packet does not match its bound source and stage-1 result",
        )
    return validated


def _validate_result_binding(value: Any) -> dict[str, Any]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    required = {
        "stage1_packet_id",
        "stage1_packet_sha256",
        "plan_id",
        "plan_sha256",
        "config_sha256",
        "input_set_sha256",
        "job_id",
        "method_id",
        "method_version",
        "unit_id",
        "attempt_id",
        "attempt_index",
    }
    require_exact_keys(row, required=required, path=path)
    return {
        "stage1_packet_id": require_string(
            row["stage1_packet_id"], path=f"{path}.stage1_packet_id"
        ),
        "stage1_packet_sha256": require_sha256(
            row["stage1_packet_sha256"], path=f"{path}.stage1_packet_sha256"
        ),
        **_validate_common_job_binding(row, path=path),
        "attempt_id": require_string(row["attempt_id"], path=f"{path}.attempt_id"),
        "attempt_index": require_int(
            row["attempt_index"], path=f"{path}.attempt_index", minimum=1
        ),
    }


def _validate_semantic_binding(value: Any) -> dict[str, Any]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    required = {
        "stage1_result_id",
        "stage1_result_sha256",
        "stage1_packet_id",
        "stage1_packet_sha256",
        "plan_id",
        "plan_sha256",
        "config_sha256",
        "input_set_sha256",
        "job_id",
        "method_id",
        "method_version",
        "unit_id",
        "source_block_id",
        "source_text_sha256",
        "back_translation_sha256",
        "presentation_id",
        "source_slot_id",
        "back_translation_slot_id",
    }
    require_exact_keys(row, required=required, path=path)
    return {
        "stage1_result_id": require_string(
            row["stage1_result_id"], path=f"{path}.stage1_result_id"
        ),
        "stage1_result_sha256": require_sha256(
            row["stage1_result_sha256"], path=f"{path}.stage1_result_sha256"
        ),
        "stage1_packet_id": require_string(
            row["stage1_packet_id"], path=f"{path}.stage1_packet_id"
        ),
        "stage1_packet_sha256": require_sha256(
            row["stage1_packet_sha256"], path=f"{path}.stage1_packet_sha256"
        ),
        **_validate_common_job_binding(row, path=path),
        "source_block_id": require_string(
            row["source_block_id"], path=f"{path}.source_block_id"
        ),
        "source_text_sha256": require_sha256(
            row["source_text_sha256"], path=f"{path}.source_text_sha256"
        ),
        "back_translation_sha256": require_sha256(
            row["back_translation_sha256"],
            path=f"{path}.back_translation_sha256",
        ),
        "presentation_id": require_string(
            row["presentation_id"], path=f"{path}.presentation_id"
        ),
        "source_slot_id": require_enum(
            row["source_slot_id"], _PASSAGE_SLOT_IDS, path=f"{path}.source_slot_id"
        ),
        "back_translation_slot_id": require_enum(
            row["back_translation_slot_id"],
            _PASSAGE_SLOT_IDS,
            path=f"{path}.back_translation_slot_id",
        ),
    }


def _validate_common_job_binding(
    row: Mapping[str, Any], *, path: str
) -> dict[str, str]:
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
        "method_id": require_enum(row["method_id"], {"sf_bt"}, path=f"{path}.method_id"),
        "method_version": require_string(
            row["method_version"], path=f"{path}.method_version"
        ),
        "unit_id": require_string(row["unit_id"], path=f"{path}.unit_id"),
    }


def _validate_prompt_binding(value: Any) -> dict[str, str]:
    path = "$.prompt"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "candidate_id",
            "prompt_sha256",
            "context_profile",
            "rendered_prompt_sha256",
        },
        path=path,
    )
    return {
        "candidate_id": require_enum(
            row["candidate_id"], {SF_BT_REVERSE_CANDIDATE_ID}, path=f"{path}.candidate_id"
        ),
        "prompt_sha256": require_enum(
            row["prompt_sha256"],
            {SF_BT_REVERSE_PROMPT_SHA256},
            path=f"{path}.prompt_sha256",
        ),
        "context_profile": require_enum(
            row["context_profile"],
            _CONTEXT_PROFILES,
            path=f"{path}.context_profile",
        ),
        "rendered_prompt_sha256": require_sha256(
            row["rendered_prompt_sha256"],
            path=f"{path}.rendered_prompt_sha256",
        ),
    }


def _validate_model_profile(value: Any) -> dict[str, str]:
    path = "$.model_profile"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "provider_id",
            "model_id",
            "model_version",
            "model_family",
            "profile_sha256",
        },
        path=path,
    )
    return {
        "provider_id": require_string(row["provider_id"], path=f"{path}.provider_id"),
        "model_id": require_string(row["model_id"], path=f"{path}.model_id"),
        "model_version": require_string(
            row["model_version"], path=f"{path}.model_version"
        ),
        "model_family": require_string(
            row["model_family"], path=f"{path}.model_family"
        ),
        "profile_sha256": require_sha256(
            row["profile_sha256"], path=f"{path}.profile_sha256"
        ),
    }


def _validate_transport(value: Any) -> dict[str, str]:
    path = "$.transport"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"completion_status", "finish_reason", "raw_response_sha256"},
        path=path,
    )
    return {
        "completion_status": require_enum(
            row["completion_status"], {"complete"}, path=f"{path}.completion_status"
        ),
        "finish_reason": require_string(
            row["finish_reason"], path=f"{path}.finish_reason"
        ),
        "raw_response_sha256": require_sha256(
            row["raw_response_sha256"], path=f"{path}.raw_response_sha256"
        ),
    }


def _validate_back_translation_output(value: Any) -> dict[str, str]:
    path = "$.output"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"back_translation"}, path=path)
    return {
        "back_translation": require_string(
            row["back_translation"], path=f"{path}.back_translation"
        )
    }


def _validate_passages(value: Any) -> list[dict[str, str]]:
    path = "$.passages"
    rows = require_list(value, path=path)
    if len(rows) != 2:
        raise ContractValidationError(
            "passage_count", path, "semantic comparison requires exactly two passages"
        )
    result: list[dict[str, str]] = []
    for index, value in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value, path=row_path)
        require_exact_keys(
            row, required={"slot_id", "text", "text_sha256"}, path=row_path
        )
        text = require_string(row["text"], path=f"{row_path}.text")
        digest = require_sha256(
            row["text_sha256"], path=f"{row_path}.text_sha256"
        )
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise ContractValidationError(
                "passage_hash",
                f"{row_path}.text_sha256",
                "passage text hash does not match its canonical text",
            )
        result.append(
            {
                "slot_id": require_enum(
                    row["slot_id"], _PASSAGE_SLOT_IDS, path=f"{row_path}.slot_id"
                ),
                "text": text,
                "text_sha256": digest,
            }
        )
    if [row["slot_id"] for row in result] != ["passage_a", "passage_b"]:
        raise ContractValidationError(
            "passage_order", path, "passages must be ordered passage_a then passage_b"
        )
    return result


def _validate_semantic_shape(packet: Mapping[str, Any]) -> None:
    binding = packet["binding"]
    if binding["source_slot_id"] == binding["back_translation_slot_id"]:
        raise ContractValidationError(
            "slot_binding",
            "$.binding",
            "source and back-translation must occupy different slots",
        )
    by_slot = {row["slot_id"]: row for row in packet["passages"]}
    if (
        by_slot[binding["source_slot_id"]]["text_sha256"]
        != binding["source_text_sha256"]
    ):
        raise ContractValidationError(
            "source_slot_binding",
            "$.binding.source_slot_id",
            "source slot does not carry the bound source text",
        )
    if (
        by_slot[binding["back_translation_slot_id"]]["text_sha256"]
        != binding["back_translation_sha256"]
    ):
        raise ContractValidationError(
            "back_translation_slot_binding",
            "$.binding.back_translation_slot_id",
            "back-translation slot does not carry the bound result text",
        )


def _validate_integrity(
    value: Any, *, field: str, path: str
) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={field}, path=path)
    return {field: require_sha256(row[field], path=f"{path}.{field}")}


def _parse_back_translation_response(raw_response_text: str) -> str:
    if not isinstance(raw_response_text, str):
        raise ContractValidationError(
            "type", "$.raw_response", "raw response must be UTF-8 text"
        )
    try:
        parsed = json.loads(
            raw_response_text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError(
            "response_json", "$.raw_response", "response is not strict JSON"
        ) from exc
    row = require_mapping(parsed, path="$.raw_response")
    require_exact_keys(row, required={"back_translation"}, path="$.raw_response")
    return require_string(
        row["back_translation"], path="$.raw_response.back_translation"
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _require_stage1_packet(packet: Mapping[str, Any]) -> None:
    if packet["binding"]["method_id"] != "sf_bt" or packet["stage"] != "back_translation":
        raise ContractValidationError(
            "stage1_packet",
            "$",
            "SF-BT stage result requires a back-translation input packet",
        )


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
