"""Closed one-retry quality decision reducer for D2L translations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


POLICY_ID = "d2l_translation_publication_eligibility_v1"

PASS_FIRST = "PASS_FIRST"
PASS_AFTER_RETRY = "PASS_AFTER_RETRY"
RETRY_TARGETED = "RETRY_TARGETED"
HOLD = "HOLD"
HOLD_TRANSLATION_CONTRACT = "HOLD_TRANSLATION_CONTRACT"
HOLD_AUDIT_CONTRACT = "HOLD_AUDIT_CONTRACT"

AUDIT_NOT_RUN = "not_run"
AUDIT_VALID = "valid"
AUDIT_INVALID_EXHAUSTED = "invalid_exhausted"

REPAIR_NONE = "none"
REPAIR_MECHANICAL = "mechanical"
REPAIR_SEMANTIC = "semantic"


@dataclass(frozen=True)
class QualityDecision:
    policy_id: str
    state: str
    publication_ready: bool
    translation_retry_requested: bool
    hold_branch: str | None
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "state": self.state,
            "publication_ready": self.publication_ready,
            "translation_retry_requested": self.translation_retry_requested,
            "hold_branch": self.hold_branch,
            "reason_codes": list(self.reason_codes),
        }


def reduce_quality_decision(
    *,
    candidate_auditable: bool,
    translation_retry_used: bool,
    audit_contract_status: str,
    deterministic_major_findings: Sequence[Mapping[str, Any]] = (),
    auditor_findings: Sequence[Mapping[str, Any]] = (),
    repair_origin: str = REPAIR_NONE,
) -> QualityDecision:
    """Compute authority from validated facts; never infer semantic correctness."""

    if audit_contract_status not in {
        AUDIT_NOT_RUN,
        AUDIT_VALID,
        AUDIT_INVALID_EXHAUSTED,
    }:
        raise ValueError(f"Unknown audit contract status: {audit_contract_status}")
    if repair_origin not in {REPAIR_NONE, REPAIR_MECHANICAL, REPAIR_SEMANTIC}:
        raise ValueError(f"Unknown repair origin: {repair_origin}")
    if translation_retry_used and repair_origin == REPAIR_NONE:
        raise ValueError("A used translation retry requires a repair origin")
    if not translation_retry_used and repair_origin != REPAIR_NONE:
        raise ValueError("An unused translation retry cannot have a repair origin")

    deterministic = list(deterministic_major_findings)
    auditor = list(auditor_findings)
    _require_major_rows(deterministic, "deterministic")
    _require_auditor_rows(auditor)

    if not candidate_auditable:
        if audit_contract_status != AUDIT_NOT_RUN or auditor:
            raise ValueError("Unauditable candidates cannot carry an Auditor result")
        if not translation_retry_used:
            return _decision(
                RETRY_TARGETED,
                retry=True,
                reasons=("candidate_unauditable",),
            )
        return _decision(
            HOLD_TRANSLATION_CONTRACT,
            hold_branch="hold_translation_contract",
            reasons=("candidate_unauditable_after_retry",),
        )

    if audit_contract_status == AUDIT_NOT_RUN:
        raise ValueError("An auditable publication candidate requires an Auditor result")
    if audit_contract_status == AUDIT_INVALID_EXHAUSTED:
        if auditor:
            raise ValueError("An invalid exhausted audit cannot carry validated findings")
        return _decision(
            HOLD_AUDIT_CONTRACT,
            hold_branch="hold_audit_contract",
            reasons=("audit_contract_invalid_after_reask",),
        )

    major_auditor = [row for row in auditor if row.get("severity") == "major"]
    reason_codes = tuple(
        [str(row.get("issue_type")) for row in deterministic]
        + [str(row.get("issue_type")) for row in major_auditor]
    )
    has_major = bool(deterministic or major_auditor)
    if has_major and not translation_retry_used:
        return _decision(RETRY_TARGETED, retry=True, reasons=reason_codes)
    if has_major:
        if deterministic:
            return _decision(
                HOLD_TRANSLATION_CONTRACT,
                hold_branch="hold_translation_contract",
                reasons=reason_codes,
            )
        branch = (
            "mechanical_repair_then_semantic_hold"
            if repair_origin == REPAIR_MECHANICAL
            else "semantic_retry_then_hold"
        )
        return _decision(HOLD, hold_branch=branch, reasons=reason_codes)

    return _decision(PASS_AFTER_RETRY if translation_retry_used else PASS_FIRST)


def _require_major_rows(rows: Sequence[Mapping[str, Any]], owner: str) -> None:
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"{owner} finding {index} must be an object")
        if row.get("severity") != "major":
            raise ValueError(f"{owner} finding {index} must be major")
        if not isinstance(row.get("issue_type"), str) or not row["issue_type"]:
            raise ValueError(f"{owner} finding {index} requires issue_type")


def _require_auditor_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"Auditor finding {index} must be an object")
        if row.get("severity") not in {"major", "advisory"}:
            raise ValueError(f"Auditor finding {index} has invalid severity")
        if not isinstance(row.get("issue_type"), str) or not row["issue_type"]:
            raise ValueError(f"Auditor finding {index} requires issue_type")


def _decision(
    state: str,
    *,
    retry: bool = False,
    hold_branch: str | None = None,
    reasons: Sequence[str] = (),
) -> QualityDecision:
    publication_ready = state in {PASS_FIRST, PASS_AFTER_RETRY}
    return QualityDecision(
        policy_id=POLICY_ID,
        state=state,
        publication_ready=publication_ready,
        translation_retry_requested=retry,
        hold_branch=hold_branch,
        reason_codes=tuple(reasons),
    )


__all__ = [
    "AUDIT_INVALID_EXHAUSTED",
    "AUDIT_NOT_RUN",
    "AUDIT_VALID",
    "HOLD",
    "HOLD_AUDIT_CONTRACT",
    "HOLD_TRANSLATION_CONTRACT",
    "PASS_AFTER_RETRY",
    "PASS_FIRST",
    "POLICY_ID",
    "REPAIR_MECHANICAL",
    "REPAIR_NONE",
    "REPAIR_SEMANTIC",
    "RETRY_TARGETED",
    "QualityDecision",
    "reduce_quality_decision",
]
