from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_commit,
    require_enum,
    require_mapping,
    require_rfc3339,
    require_sha256,
    require_string,
)
from pipeline.eval.scorer_input_packets_v1 import (
    seal_scorer_input_packet,
    validate_scorer_input_packet,
)
from pipeline.eval.sf_bt_stage_contracts_v1 import (
    SF_BT_SEMANTIC_PACKET_SCHEMA_ID,
    SF_BT_SEMANTIC_PACKET_SCHEMA_VERSION,
    seal_sf_bt_semantic_judge_packet,
    validate_sf_back_translation_result,
    validate_sf_bt_semantic_judge_packet,
)


__all__ = [
    "build_sf_bt_probe_semantic_packet_v1",
    "build_sf_bt_probe_stage1_packet_v1",
]


_CONTEXT_PROFILES = frozenset({"no_context", "bounded_neighbors"})
_P2_STRATUM = "P2_omission_control"


def build_sf_bt_probe_stage1_packet_v1(
    case: Mapping[str, Any],
    *,
    fixture_sha256: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    """Project one approved P2 row into the ordinary SF-BT stage-1 contract."""

    row = _validate_p2_case(case)
    fixture_hash = require_sha256(fixture_sha256, path="$.fixture_sha256")
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    case_id = row["case_id"]
    blocks = []
    for suffix, role, text in (
        ("preceding", "preceding", row["target_preceding_vi"]),
        ("active", "active", row["target_active_vi"]),
        ("following", "following", row["target_following_vi"]),
    ):
        blocks.append(
            {
                "block_id": f"probe-{case_id}-{suffix}",
                "role": role,
                "block_type": "paragraph",
                "status": "translated" if text is not None else "missing",
                "text": text,
            }
        )
    plan_sha256 = _digest("eval-sfbt-p2-plan-v1", fixture_hash)
    config_sha256 = _digest(
        "eval-sfbt-p2-config-v1", "no_context", "bounded_neighbors"
    )
    return validate_scorer_input_packet(
        seal_scorer_input_packet(
            {
                "schema_id": "EvaluationScorerInputPacketV1",
                "schema_version": "1.0.0",
                "packet_id": f"probe-sfbt-p2-{case_id}",
                "created_at": timestamp,
                "producer": {
                    "workstream": "evaluation",
                    "component": "scorer_probe_packets_v1",
                    "component_version": "1.0.0",
                    "code_commit": commit,
                },
                "binding": {
                    "plan_id": "eval-sfbt-p2-plan-v1",
                    "plan_sha256": plan_sha256,
                    "config_sha256": config_sha256,
                    "input_set_sha256": fixture_hash,
                    "job_id": f"eval-sfbt-p2-job-{case_id}",
                    "method_id": "sf_bt",
                    "method_version": "sf_bt_v3_1_p2_probe_v1",
                    "unit_id": f"eval-sfbt-p2-unit-{case_id}",
                },
                "languages": {
                    "source_language": "en",
                    "target_language": "vi",
                },
                "stage": "back_translation",
                "source": None,
                "candidates": [
                    {"slot_id": "candidate_1", "blocks": blocks}
                ],
                "integrity": {"packet_sha256": "0" * 64},
            }
        )
    )


def build_sf_bt_probe_semantic_packet_v1(
    case: Mapping[str, Any],
    stage1_packet: Mapping[str, Any],
    stage1_result: Mapping[str, Any],
    *,
    context_profile: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    """Build the standard stage-2 packet without exposing fixture oracle fields."""

    row = _validate_p2_case(case)
    packet = validate_scorer_input_packet(stage1_packet)
    result = validate_sf_back_translation_result(stage1_result)
    profile = require_enum(
        context_profile, _CONTEXT_PROFILES, path="$.context_profile"
    )
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    if result["binding"]["stage1_packet_sha256"] != packet["integrity"][
        "packet_sha256"
    ]:
        raise ContractValidationError(
            "stage1_binding",
            "$.stage1_result.binding.stage1_packet_sha256",
            "stage-1 result references another probe packet",
        )
    if result["prompt"]["context_profile"] != profile:
        raise ContractValidationError(
            "context_profile",
            "$.stage1_result.prompt.context_profile",
            "stage-1 result references another context profile",
        )
    source_text = row["source_active_en"]
    back_translation = result["output"]["back_translation"]
    source_first = int(_digest(row["case_id"])[0], 16) % 2 == 0
    source_slot_id = "passage_a" if source_first else "passage_b"
    back_slot_id = "passage_b" if source_first else "passage_a"
    passage_text = {
        source_slot_id: source_text,
        back_slot_id: back_translation,
    }
    result_sha256 = result["integrity"]["result_sha256"]
    packet_id = "probe-sfbt-p2-judge-" + _digest(
        row["case_id"], profile, result_sha256
    )[:24]
    semantic = seal_sf_bt_semantic_judge_packet(
        {
            "schema_id": SF_BT_SEMANTIC_PACKET_SCHEMA_ID,
            "schema_version": SF_BT_SEMANTIC_PACKET_SCHEMA_VERSION,
            "packet_id": packet_id,
            "created_at": timestamp,
            "producer": {
                "workstream": "evaluation",
                "component": "scorer_probe_packets_v1",
                "component_version": "1.0.0",
                "code_commit": commit,
            },
            "binding": {
                "stage1_result_id": result["result_id"],
                "stage1_result_sha256": result_sha256,
                "stage1_packet_id": packet["packet_id"],
                "stage1_packet_sha256": packet["integrity"]["packet_sha256"],
                **packet["binding"],
                "source_block_id": f"probe-{row['case_id']}-active",
                "source_text_sha256": _digest(source_text),
                "back_translation_sha256": _digest(back_translation),
                "presentation_id": (
                    "probe_source_first" if source_first else "probe_reverse_first"
                ),
                "source_slot_id": source_slot_id,
                "back_translation_slot_id": back_slot_id,
            },
            "language": "en",
            "stage": "semantic_comparison",
            "passages": [
                {
                    "slot_id": slot_id,
                    "text": passage_text[slot_id],
                    "text_sha256": _digest(passage_text[slot_id]),
                }
                for slot_id in ("passage_a", "passage_b")
            ],
            "integrity": {"packet_sha256": "0" * 64},
        }
    )
    return validate_sf_bt_semantic_judge_packet(semantic)


def _validate_p2_case(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$.case")
    stratum = require_enum(row.get("stratum"), {_P2_STRATUM}, path="$.case.stratum")
    return {
        "case_id": require_string(row.get("case_id"), path="$.case.case_id"),
        "stratum": stratum,
        "source_active_en": require_string(
            row.get("source_active_en"), path="$.case.source_active_en"
        ),
        "target_preceding_vi": _nullable_text(
            row.get("target_preceding_vi"), "$.case.target_preceding_vi"
        ),
        "target_active_vi": require_string(
            row.get("target_active_vi"), path="$.case.target_active_vi"
        ),
        "target_following_vi": _nullable_text(
            row.get("target_following_vi"), "$.case.target_following_vi"
        ),
    }


def _nullable_text(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return require_string(value, path=path)


def _digest(*parts: str) -> str:
    return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
