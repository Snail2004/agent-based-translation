from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.step5_types import CanonicalRecord, Step5ContractError


class PreregisterError(Step5ContractError):
    """Raised when oracle groups leak across preregistered slices."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OracleGroup(CanonicalRecord):
    oracle_group_id: str
    member_row_ids: frozenset[str]
    group_key: str

    def __post_init__(self) -> None:
        if not self.oracle_group_id or not self.group_key or not self.member_row_ids:
            raise PreregisterError("oracle groups require an id, key, and member rows")


@dataclass(frozen=True, slots=True, kw_only=True)
class OracleSplit(CanonicalRecord):
    split_manifest_hash: str = field(default="", metadata={"canonical_exclude": True})
    qualify_groups: frozenset[str]
    dev_eval_groups: frozenset[str]
    held_out_commitment_hash: str

    self_hash_field = "split_manifest_hash"


@dataclass(frozen=True, slots=True, kw_only=True)
class SealedHeldOutPayload:
    commitment_hash: str
    canonical_payload: bytes


def held_out_commitment(rows: tuple[Mapping[str, Any], ...]) -> str:
    normalized = sorted(
        (dict(row) for row in rows), key=lambda row: str(row.get("row_id") or "")
    )
    ids = [str(row.get("row_id") or "") for row in normalized]
    if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise PreregisterError("held-out rows require unique non-empty row ids")
    return canonical_hash(normalized)


def seal_held_out_rows(
    rows: tuple[Mapping[str, Any], ...],
) -> SealedHeldOutPayload:
    normalized = sorted(
        (dict(row) for row in rows), key=lambda row: str(row.get("row_id") or "")
    )
    commitment = held_out_commitment(tuple(normalized))
    return SealedHeldOutPayload(
        commitment_hash=commitment,
        canonical_payload=canonical_json(normalized).encode("utf-8"),
    )


def preregister_oracle_split(
    *,
    groups: tuple[OracleGroup, ...],
    qualify_group_ids: frozenset[str],
    dev_eval_group_ids: frozenset[str],
    held_out_group_ids: frozenset[str],
    held_out_rows: tuple[Mapping[str, Any], ...],
) -> tuple[OracleSplit, SealedHeldOutPayload]:
    by_id = {group.oracle_group_id: group for group in groups}
    if len(by_id) != len(groups):
        raise PreregisterError("oracle group ids must be unique")
    slices = (qualify_group_ids, dev_eval_group_ids, held_out_group_ids)
    if any(slices[left] & slices[right] for left in range(3) for right in range(left + 1, 3)):
        raise PreregisterError("oracle group slices overlap")
    assigned = qualify_group_ids | dev_eval_group_ids | held_out_group_ids
    if assigned != frozenset(by_id):
        raise PreregisterError("oracle split is not an exact group partition")

    row_owner: dict[str, str] = {}
    key_slice: dict[str, int] = {}
    for slice_index, group_ids in enumerate(slices):
        for group_id in group_ids:
            group = by_id[group_id]
            prior_slice = key_slice.setdefault(group.group_key, slice_index)
            if prior_slice != slice_index:
                raise PreregisterError("shared oracle group_key straddles slices")
            for row_id in group.member_row_ids:
                if row_id in row_owner:
                    raise PreregisterError("oracle row belongs to multiple groups")
                row_owner[row_id] = group_id

    held_row_ids = frozenset(
        row_id
        for group_id in held_out_group_ids
        for row_id in by_id[group_id].member_row_ids
    )
    payload_row_ids = frozenset(str(row.get("row_id") or "") for row in held_out_rows)
    if held_row_ids != payload_row_ids:
        raise PreregisterError("held-out payload is not an exact held-out row cover")
    sealed = seal_held_out_rows(held_out_rows)
    draft = OracleSplit(
        qualify_groups=qualify_group_ids,
        dev_eval_groups=dev_eval_group_ids,
        held_out_commitment_hash=sealed.commitment_hash,
    )
    split = OracleSplit(
        split_manifest_hash=draft.canonical_hash(),
        qualify_groups=draft.qualify_groups,
        dev_eval_groups=draft.dev_eval_groups,
        held_out_commitment_hash=draft.held_out_commitment_hash,
    )
    return split, sealed


def public_split_manifest(split: OracleSplit) -> dict[str, Any]:
    if split.canonical_hash() != split.split_manifest_hash:
        raise PreregisterError("oracle split manifest hash mismatch")
    payload = split.to_canonical_payload()
    payload["split_manifest_hash"] = split.split_manifest_hash
    if "held_out_groups" in payload or "held_out_payload" in payload:
        raise PreregisterError("public split manifest leaks held-out data")
    return payload


__all__ = [
    "OracleGroup",
    "OracleSplit",
    "PreregisterError",
    "SealedHeldOutPayload",
    "held_out_commitment",
    "preregister_oracle_split",
    "public_split_manifest",
    "seal_held_out_rows",
]
