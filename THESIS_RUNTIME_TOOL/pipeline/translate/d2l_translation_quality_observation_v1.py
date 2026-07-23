"""Nonblocking, content-addressed observations for translation quality audit."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from pipeline.prepass.d2l_console_replay_contract_v1 import canonical_sha256


SCHEMA_VERSION = "d2l_translation_quality_observation_v1"
POLICY_ID = "d2l_translation_quality_nonblocking_v1"

_FINDING_KEYS = {
    "block_id",
    "issue_type",
    "severity",
    "source_evidence",
    "target_evidence",
    "reason",
}
_FORBIDDEN_KEYS = {
    "raw_prompt",
    "raw_response",
    "prompt_text",
    "response_text",
    "api_key",
    "secret",
    "gold",
    "oracle",
    "reference_text",
}


class D2LQualityObservationError(ValueError):
    """Raised when a quality observation cannot be relayed truthfully."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise D2LQualityObservationError(f"{label} must be an object")
    return dict(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise D2LQualityObservationError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise D2LQualityObservationError(f"{label} must be a string array")
    if len(value) != len(set(value)):
        raise D2LQualityObservationError(f"{label} must not contain duplicates")
    return list(value)


def _reject_forbidden(value: Any, label: str = "quality_observation") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise D2LQualityObservationError(f"{label} contains forbidden key: {key}")
            _reject_forbidden(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{label}[{index}]")


def _finding(value: Any, label: str) -> dict[str, str]:
    row = _mapping(value, label)
    if set(row) != _FINDING_KEYS:
        raise D2LQualityObservationError(f"{label} keys mismatch")
    result = {
        key: _string(row[key], f"{label}.{key}")
        for key in ("block_id", "issue_type", "severity", "reason")
    }
    for key in ("source_evidence", "target_evidence"):
        if not isinstance(row[key], str):
            raise D2LQualityObservationError(f"{label}.{key} must be a string")
        result[key] = row[key]
    if not result["source_evidence"] and result["issue_type"] != "unsupported_addition":
        raise D2LQualityObservationError(f"{label}.source_evidence is required")
    if not result["target_evidence"] and result["issue_type"] != "meaning_omission":
        raise D2LQualityObservationError(f"{label}.target_evidence is required")
    return {key: result[key] for key in sorted(_FINDING_KEYS)}


def build_quality_observation(
    *,
    audited_block_ids: Sequence[str],
    findings: Sequence[Mapping[str, Any]],
    source_translation_artifact_refs: Sequence[str],
) -> dict[str, Any]:
    block_ids = _string_list(list(audited_block_ids), "audited_block_ids")
    refs = _string_list(
        list(source_translation_artifact_refs), "source_translation_artifact_refs"
    )
    normalized_findings = [
        _finding(value, f"findings[{index}]") for index, value in enumerate(findings)
    ]
    known = set(block_ids)
    if any(row["block_id"] not in known for row in normalized_findings):
        raise D2LQualityObservationError("quality finding references a foreign block")
    grouped = {block_id: [] for block_id in block_ids}
    for row in normalized_findings:
        grouped[row["block_id"]].append(row)
    blocks = []
    for block_id in block_ids:
        rows = grouped[block_id]
        blocks.append(
            {
                "block_id": block_id,
                "quality_status": "issue" if rows else "pass",
                "finding_count": len(rows),
                "findings": rows,
                "continue_to_scoring": True,
            }
        )
    report = {
        "schema": SCHEMA_VERSION,
        "policy_id": POLICY_ID,
        "mechanical_validation_status": "passed",
        "audited_block_ids": block_ids,
        "source_translation_artifact_refs": refs,
        "blocks": blocks,
        "counts": {
            "pass": sum(row["quality_status"] == "pass" for row in blocks),
            "issue": sum(row["quality_status"] == "issue" for row in blocks),
            "findings": len(normalized_findings),
            "total": len(blocks),
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    validate_quality_observation(report)
    return report


def validate_quality_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(value, "quality_observation")
    _reject_forbidden(row)
    expected = {
        "schema",
        "policy_id",
        "mechanical_validation_status",
        "audited_block_ids",
        "source_translation_artifact_refs",
        "blocks",
        "counts",
        "report_sha256",
    }
    if set(row) != expected or row["schema"] != SCHEMA_VERSION:
        raise D2LQualityObservationError("quality observation shape is invalid")
    if row["policy_id"] != POLICY_ID:
        raise D2LQualityObservationError("quality observation policy is invalid")
    if row["mechanical_validation_status"] != "passed":
        raise D2LQualityObservationError(
            "mechanically invalid translations cannot enter quality observation"
        )
    block_ids = _string_list(row["audited_block_ids"], "audited_block_ids")
    _string_list(
        row["source_translation_artifact_refs"], "source_translation_artifact_refs"
    )
    if not isinstance(row["blocks"], list):
        raise D2LQualityObservationError("quality observation blocks must be an array")
    if [item.get("block_id") for item in row["blocks"] if isinstance(item, Mapping)] != block_ids:
        raise D2LQualityObservationError("quality observation does not exact-cover block order")
    total_findings = 0
    pass_count = 0
    issue_count = 0
    for index, raw in enumerate(row["blocks"]):
        block = _mapping(raw, f"blocks[{index}]")
        if set(block) != {
            "block_id",
            "quality_status",
            "finding_count",
            "findings",
            "continue_to_scoring",
        }:
            raise D2LQualityObservationError(f"blocks[{index}] keys mismatch")
        if block["continue_to_scoring"] is not True:
            raise D2LQualityObservationError("semantic quality findings must remain nonblocking")
        if not isinstance(block["findings"], list):
            raise D2LQualityObservationError(f"blocks[{index}].findings must be an array")
        findings = [
            _finding(item, f"blocks[{index}].findings[{finding_index}]")
            for finding_index, item in enumerate(block["findings"])
        ]
        if any(item["block_id"] != block["block_id"] for item in findings):
            raise D2LQualityObservationError("quality finding block lineage mismatch")
        if isinstance(block["finding_count"], bool) or block["finding_count"] != len(findings):
            raise D2LQualityObservationError("quality finding count mismatch")
        expected_status = "issue" if findings else "pass"
        if block["quality_status"] != expected_status:
            raise D2LQualityObservationError("quality block status does not match findings")
        pass_count += expected_status == "pass"
        issue_count += expected_status == "issue"
        total_findings += len(findings)
    counts = _mapping(row["counts"], "quality_observation.counts")
    actual_counts = {
        "pass": pass_count,
        "issue": issue_count,
        "findings": total_findings,
        "total": len(block_ids),
    }
    if counts != actual_counts:
        raise D2LQualityObservationError("quality observation counts mismatch")
    unsigned = dict(row)
    unsigned.pop("report_sha256", None)
    if canonical_sha256(unsigned) != _string(row["report_sha256"], "report_sha256").upper():
        raise D2LQualityObservationError("quality observation hash drift")
    return row


__all__ = [
    "D2LQualityObservationError",
    "POLICY_ID",
    "SCHEMA_VERSION",
    "build_quality_observation",
    "validate_quality_observation",
]
