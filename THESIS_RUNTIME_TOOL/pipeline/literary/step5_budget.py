from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, TypeAlias

from pipeline.literary.step5_types import CanonicalRecord, DecisionKind, Step5ContractError


ExecutionCostClass: TypeAlias = Literal["remote_quota", "local_compute"]
CALL_SYMBOLS = frozenset({"R", "J", "Cs", "Cd", "F", "P", "A", "De", "Dc", "X"})
REMOTE_DAILY_LIMIT = 225_000


class BudgetContractError(Step5ContractError):
    """Raised for malformed or over-budget S5 call plans."""


class BudgetExceededError(BudgetContractError):
    """Raised when a preflight or pre-apply quota gate fails."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CallPlanEntry(CanonicalRecord):
    call_plan_entry_id: str
    call_kind: str
    decision_kind: DecisionKind
    shard_id: str | None
    authority_route_id: str
    execution_cost_class: ExecutionCostClass
    quota_bucket_id: str | None
    model_id: str
    prompt_tokens_estimate: int
    max_output_tokens: int
    technical_retry_cap: Literal[0, 1]

    def __post_init__(self) -> None:
        if self.call_kind not in CALL_SYMBOLS:
            raise BudgetContractError(f"unknown call-count symbol: {self.call_kind}")
        if self.prompt_tokens_estimate < 0 or self.max_output_tokens < 0:
            raise BudgetContractError("call token estimates cannot be negative")
        if self.technical_retry_cap not in {0, 1}:
            raise BudgetContractError("technical retry cap must be zero or one")
        if self.execution_cost_class == "remote_quota" and not self.quota_bucket_id:
            raise BudgetContractError("remote calls require a quota bucket")
        if self.execution_cost_class == "local_compute" and self.quota_bucket_id is not None:
            raise BudgetContractError("local calls cannot carry a remote quota bucket")
        if not self.call_plan_entry_id or not self.authority_route_id or not self.model_id:
            raise BudgetContractError("call-plan identity fields must be non-empty")

    @property
    def fresh_upper_bound(self) -> int:
        return self.prompt_tokens_estimate + self.max_output_tokens

    @property
    def retry_reserve(self) -> int:
        return self.fresh_upper_bound * self.technical_retry_cap


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageSnapshot(CanonicalRecord):
    quota_bucket_id: str
    model_id: str
    utc_day: str
    prompt_plus_completion_used: int

    def __post_init__(self) -> None:
        if self.prompt_plus_completion_used < 0:
            raise BudgetContractError("usage cannot be negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class CallPlanManifest(CanonicalRecord):
    entries: tuple[CallPlanEntry, ...]
    finite_caps: Mapping[str, int]
    manifest_hash: str = field(default="", metadata={"canonical_exclude": True})

    self_hash_field = "manifest_hash"


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountingTotals(CanonicalRecord):
    restored_tokens: int
    cache_tokens: int
    this_attempt_tokens: int
    combined_tokens: int

    def __post_init__(self) -> None:
        values = (
            self.restored_tokens,
            self.cache_tokens,
            self.this_attempt_tokens,
            self.combined_tokens,
        )
        if any(value < 0 for value in values):
            raise BudgetContractError("accounting totals cannot be negative")
        if self.combined_tokens != sum(values[:3]):
            raise BudgetContractError("combined accounting must equal its three sources")


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetGateReport(CanonicalRecord):
    utc_day: str
    remote_totals: Mapping[str, int]
    local_compute_totals: Mapping[str, int]
    passed: bool


def seal_call_plan(
    *, entries: tuple[CallPlanEntry, ...], finite_caps: Mapping[str, int]
) -> CallPlanManifest:
    draft = CallPlanManifest(entries=entries, finite_caps=dict(finite_caps))
    sealed = CallPlanManifest(
        entries=entries,
        finite_caps=dict(finite_caps),
        manifest_hash=draft.canonical_hash(),
    )
    validate_call_plan(sealed)
    return sealed


def validate_call_plan(manifest: CallPlanManifest) -> None:
    if manifest.canonical_hash() != manifest.manifest_hash:
        raise BudgetContractError("call-plan manifest hash mismatch")
    if set(manifest.finite_caps) != CALL_SYMBOLS:
        raise BudgetContractError("finite call caps must cover the closed symbol table")
    if any(not isinstance(value, int) or value < 0 for value in manifest.finite_caps.values()):
        raise BudgetContractError("finite call caps must be non-negative integers")
    entry_ids = [entry.call_plan_entry_id for entry in manifest.entries]
    if len(entry_ids) != len(set(entry_ids)):
        raise BudgetContractError("call-plan entry ids must be unique")
    counts = {symbol: 0 for symbol in CALL_SYMBOLS}
    for entry in manifest.entries:
        counts[entry.call_kind] += 1
    over = [key for key, count in counts.items() if count > manifest.finite_caps[key]]
    if over:
        raise BudgetContractError(f"call plan exceeds finite caps: {sorted(over)}")


def _plan_entries(*manifests: CallPlanManifest) -> tuple[CallPlanEntry, ...]:
    all_entries: list[CallPlanEntry] = []
    seen: set[str] = set()
    for manifest in manifests:
        validate_call_plan(manifest)
        for entry in manifest.entries:
            if entry.call_plan_entry_id in seen:
                raise BudgetContractError("duplicate entry id across call-plan manifests")
            seen.add(entry.call_plan_entry_id)
            all_entries.append(entry)
    return tuple(all_entries)


def budget_gate(
    *,
    utc_day: str,
    usage: tuple[UsageSnapshot, ...],
    deterministic_base: CallPlanManifest,
    contingent_reserve: CallPlanManifest,
    remote_limit: int = REMOTE_DAILY_LIMIT,
) -> BudgetGateReport:
    if remote_limit <= 0:
        raise BudgetContractError("remote quota limit must be positive")
    entries = _plan_entries(deterministic_base, contingent_reserve)
    remote: dict[str, int] = {}
    local: dict[str, int] = {}
    for snapshot in usage:
        if snapshot.utc_day != utc_day:
            continue
        key = f"{snapshot.quota_bucket_id}|{snapshot.model_id}"
        remote[key] = remote.get(key, 0) + snapshot.prompt_plus_completion_used
    for entry in entries:
        upper = entry.fresh_upper_bound + entry.retry_reserve
        if entry.execution_cost_class == "remote_quota":
            key = f"{entry.quota_bucket_id}|{entry.model_id}"
            remote[key] = remote.get(key, 0) + upper
        else:
            local[entry.model_id] = local.get(entry.model_id, 0) + upper
    passed = all(total <= remote_limit for total in remote.values())
    report = BudgetGateReport(
        utc_day=utc_day,
        remote_totals=dict(sorted(remote.items())),
        local_compute_totals=dict(sorted(local.items())),
        passed=passed,
    )
    if not passed:
        raise BudgetExceededError(
            f"remote quota gate exceeded: {report.remote_totals}"
        )
    return report


def preflight_gate(**kwargs: object) -> BudgetGateReport:
    return budget_gate(**kwargs)  # type: ignore[arg-type]


def pre_apply_gate(**kwargs: object) -> BudgetGateReport:
    return budget_gate(**kwargs)  # type: ignore[arg-type]


__all__ = [
    "AccountingTotals",
    "BudgetContractError",
    "BudgetExceededError",
    "BudgetGateReport",
    "CALL_SYMBOLS",
    "CallPlanEntry",
    "CallPlanManifest",
    "ExecutionCostClass",
    "REMOTE_DAILY_LIMIT",
    "UsageSnapshot",
    "budget_gate",
    "pre_apply_gate",
    "preflight_gate",
    "seal_call_plan",
    "validate_call_plan",
]
