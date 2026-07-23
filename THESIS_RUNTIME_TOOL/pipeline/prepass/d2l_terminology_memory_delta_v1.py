"""Durable run-local glossary seal and committed terminology deltas."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
)


SEALED_GLOSSARY_SCHEMA = "d2l_sealed_glossary_v1"
COMMIT_RECEIPT_SCHEMA = "d2l_glossary_commit_receipt_v1"
MEMORY_DELTA_CONTRACT = "memory_delta_v1"
MEMORY_DELTA_BATCH_SCHEMA = "d2l_terminology_memory_delta_batch_v1"
COMMIT_PACKAGE_SCHEMA = "d2l_glossary_commit_package_v1"
GLOSSARY_DRAFT_VERSION = "d2l_b2_glossary_draft_v2"

_ENTRY_KEYS = {
    "entry_id",
    "canonical_source",
    "canonical_target_vi",
    "alternative_targets",
    "surfaces",
    "chapter_id",
    "status",
    "directive",
    "canonical_applicability",
    "evidence_block_ids",
    "evidence_complete",
    "source_member_candidate_ids",
    "decision_rationale",
    "pending_target_proposals",
    "rejected_target_proposals",
    "resolution",
    "source_lineage",
}
_EVIDENCE_KEYS = {
    "evidence_block_ids",
    "evidence_complete",
    "source_member_candidate_ids",
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


class D2LTerminologyDeltaError(ValueError):
    """Raised when a glossary commit cannot be proven mechanically."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise D2LTerminologyDeltaError(f"{label} must be an object")
    return dict(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise D2LTerminologyDeltaError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise D2LTerminologyDeltaError(f"{label} must be an integer >= {minimum}")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise D2LTerminologyDeltaError(f"{label} must be a string array")
    if len(value) != len(set(value)):
        raise D2LTerminologyDeltaError(f"{label} must not contain duplicates")
    return list(value)


def _target_variants(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise D2LTerminologyDeltaError(f"{label} must be an array")
    rows: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        row = _mapping(raw, f"{label}[{index}]")
        if set(row) != {"target_vi", "applicability"}:
            raise D2LTerminologyDeltaError(f"{label}[{index}] keys mismatch")
        target = _string(row["target_vi"], f"{label}[{index}].target_vi")
        applicability = _string(
            row["applicability"], f"{label}[{index}].applicability"
        )
        identity = (target, applicability)
        if identity in identities:
            raise D2LTerminologyDeltaError(f"{label} must not contain duplicates")
        identities.add(identity)
        rows.append({"target_vi": target, "applicability": applicability})
    return rows


def _reject_forbidden(value: Any, label: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise D2LTerminologyDeltaError(f"{label} contains forbidden key: {key}")
            _reject_forbidden(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{label}[{index}]")


def _validate_entry(value: Any, label: str) -> dict[str, Any]:
    row = _mapping(value, label)
    if set(row) != _ENTRY_KEYS:
        raise D2LTerminologyDeltaError(f"{label} keys mismatch")
    _reject_forbidden(row, label)
    _string(row["entry_id"], f"{label}.entry_id")
    _string(row["canonical_source"], f"{label}.canonical_source")
    _string(row["canonical_target_vi"], f"{label}.canonical_target_vi")
    _string(row["chapter_id"], f"{label}.chapter_id")
    _string(row["status"], f"{label}.status")
    _string(row["directive"], f"{label}.directive")
    _target_variants(row["alternative_targets"], f"{label}.alternative_targets")
    for key in (
        "surfaces",
        "evidence_block_ids",
        "source_member_candidate_ids",
    ):
        _string_list(row[key], f"{label}.{key}")
    if not isinstance(row["evidence_complete"], bool):
        raise D2LTerminologyDeltaError(f"{label}.evidence_complete must be boolean")
    for key in ("pending_target_proposals", "rejected_target_proposals"):
        if not isinstance(row[key], list):
            raise D2LTerminologyDeltaError(f"{label}.{key} must be an array")
    _mapping(row["resolution"], f"{label}.resolution")
    _mapping(row["source_lineage"], f"{label}.source_lineage")
    if row["canonical_applicability"] is not None and not isinstance(
        row["canonical_applicability"], (str, Mapping)
    ):
        raise D2LTerminologyDeltaError(
            f"{label}.canonical_applicability must be null, string or object"
        )
    if row["decision_rationale"] is not None and not isinstance(row["decision_rationale"], str):
        raise D2LTerminologyDeltaError(f"{label}.decision_rationale must be null or string")
    return row


def _validate_draft(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(value, "glossary_draft")
    _reject_forbidden(row, "glossary_draft")
    if row.get("draft_version") != GLOSSARY_DRAFT_VERSION:
        raise D2LTerminologyDeltaError("glossary draft version is unsupported")
    stored_sha = _string(row.get("draft_sha256"), "glossary_draft.draft_sha256").upper()
    unsigned = dict(row)
    unsigned.pop("draft_sha256", None)
    if canonical_sha256(unsigned) != stored_sha:
        raise D2LTerminologyDeltaError("glossary draft hash drift")
    if row.get("production_published") is not False:
        raise D2LTerminologyDeltaError("input glossary draft must remain unpublished")
    ready = row.get("ready_entries")
    pending = row.get("pending_entries")
    if not isinstance(ready, list) or not isinstance(pending, list):
        raise D2LTerminologyDeltaError("glossary draft entries must be arrays")
    normalized_ready = [_validate_entry(item, f"ready_entries[{index}]") for index, item in enumerate(ready)]
    ids = [item["entry_id"] for item in normalized_ready]
    if len(ids) != len(set(ids)):
        raise D2LTerminologyDeltaError("ready entry_id values must be unique")
    canonical_sources = [item["canonical_source"] for item in normalized_ready]
    if len(canonical_sources) != len(set(canonical_sources)):
        raise D2LTerminologyDeltaError("ready canonical_source values must be unique")
    if any(item["status"] != "ready_draft" for item in normalized_ready):
        raise D2LTerminologyDeltaError("ready glossary rows must have ready_draft status")
    counts = _mapping(row.get("counts"), "glossary_draft.counts")
    if counts.get("ready_entries") != len(normalized_ready):
        raise D2LTerminologyDeltaError("glossary draft ready count mismatch")
    if counts.get("pending_entries") != len(pending):
        raise D2LTerminologyDeltaError("glossary draft pending count mismatch")
    if counts.get("admitted_exact_cover") != len(normalized_ready) + len(pending):
        raise D2LTerminologyDeltaError("glossary draft admitted cover mismatch")
    row["ready_entries"] = normalized_ready
    return row


def _content_projection(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in sorted(_ENTRY_KEYS - _EVIDENCE_KEYS)}


def _evidence_delta(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
    before_blocks = set(before.get("evidence_block_ids") or []) if before else set()
    after_blocks = set(after.get("evidence_block_ids") or [])
    before_candidates = set(before.get("source_member_candidate_ids") or []) if before else set()
    after_candidates = set(after.get("source_member_candidate_ids") or [])
    return {
        "added_block_ids": sorted(after_blocks - before_blocks),
        "removed_block_ids": sorted(before_blocks - after_blocks),
        "added_candidate_ids": sorted(after_candidates - before_candidates),
        "removed_candidate_ids": sorted(before_candidates - after_candidates),
        "evidence_complete_before": None if before is None else before.get("evidence_complete"),
        "evidence_complete_after": after.get("evidence_complete"),
    }


def _state_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("state_sha256", None)
    return payload


def _receipt_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("receipt_sha256", None)
    return payload


def _batch_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("batch_sha256", None)
    return payload


def validate_sealed_glossary(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(value, "sealed_glossary")
    expected = {
        "schema",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "state_generation",
        "commit_scope",
        "source_draft_sha256",
        "previous_state_sha256",
        "ready_record_count",
        "pending_record_count",
        "records",
        "state_sha256",
    }
    if set(row) != expected or row["schema"] != SEALED_GLOSSARY_SCHEMA:
        raise D2LTerminologyDeltaError("sealed glossary shape is invalid")
    for key in ("workflow_run_id", "component_run_id", "commit_scope", "source_draft_sha256"):
        _string(row[key], f"sealed_glossary.{key}")
    _integer(row["component_attempt_id"], "component_attempt_id", minimum=1)
    _integer(row["state_generation"], "state_generation", minimum=1)
    _integer(row["ready_record_count"], "ready_record_count")
    _integer(row["pending_record_count"], "pending_record_count")
    if row["previous_state_sha256"] is not None:
        _string(row["previous_state_sha256"], "previous_state_sha256")
    records = row["records"]
    if not isinstance(records, list):
        raise D2LTerminologyDeltaError("sealed_glossary.records must be an array")
    seen: set[str] = set()
    canonical_sources: set[str] = set()
    for index, raw in enumerate(records):
        record = _mapping(raw, f"records[{index}]")
        if set(record) != {"record_id", "revision", "record_hash", "lifecycle", "value"}:
            raise D2LTerminologyDeltaError(f"records[{index}] keys mismatch")
        record_id = _string(record["record_id"], f"records[{index}].record_id")
        if record_id in seen:
            raise D2LTerminologyDeltaError("sealed glossary record_id values must be unique")
        seen.add(record_id)
        _integer(record["revision"], f"records[{index}].revision", minimum=1)
        if record["lifecycle"] != "committed":
            raise D2LTerminologyDeltaError("sealed glossary records must be committed")
        entry = _validate_entry(record["value"], f"records[{index}].value")
        if entry["entry_id"] != record_id:
            raise D2LTerminologyDeltaError("record_id does not match entry_id")
        if entry["canonical_source"] in canonical_sources:
            raise D2LTerminologyDeltaError("sealed glossary canonical sources must be unique")
        canonical_sources.add(entry["canonical_source"])
        if canonical_sha256(entry) != _string(record["record_hash"], "record_hash").upper():
            raise D2LTerminologyDeltaError("sealed glossary record hash drift")
    if row["ready_record_count"] != len(records):
        raise D2LTerminologyDeltaError("sealed glossary ready count mismatch")
    if canonical_sha256(_state_payload(row)) != _string(row["state_sha256"], "state_sha256").upper():
        raise D2LTerminologyDeltaError("sealed glossary state hash drift")
    return row


def validate_memory_delta(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(value, "memory_delta")
    expected = {
        "contract",
        "domain",
        "collection",
        "operation",
        "lifecycle",
        "delta_id",
        "record_id",
        "label",
        "revision_before",
        "revision_after",
        "record_hash_before",
        "record_hash_after",
        "before",
        "after",
        "evidence_delta",
        "reason_code",
        "source_refs",
        "commit_receipt",
    }
    if set(row) != expected:
        raise D2LTerminologyDeltaError("memory_delta keys mismatch")
    if row["contract"] != MEMORY_DELTA_CONTRACT or row["domain"] != "terminology":
        raise D2LTerminologyDeltaError("memory_delta authority is invalid")
    if row["collection"] != "term" or row["lifecycle"] != "committed":
        raise D2LTerminologyDeltaError("memory_delta collection/lifecycle is invalid")
    if row["operation"] not in {"added", "reinforced", "revised"}:
        raise D2LTerminologyDeltaError("memory_delta operation is invalid")
    for key in ("delta_id", "record_id", "label", "record_hash_after", "reason_code"):
        _string(row[key], f"memory_delta.{key}")
    revision_after = _integer(row["revision_after"], "revision_after", minimum=1)
    after = _validate_entry(row["after"], "memory_delta.after")
    if after["entry_id"] != row["record_id"] or after["canonical_source"] != row["label"]:
        raise D2LTerminologyDeltaError("memory_delta after identity mismatch")
    if canonical_sha256(after) != row["record_hash_after"].upper():
        raise D2LTerminologyDeltaError("memory_delta after hash drift")
    if row["operation"] == "added":
        if any(row[key] is not None for key in ("revision_before", "record_hash_before", "before")):
            raise D2LTerminologyDeltaError("added delta cannot have before state")
        if revision_after != 1:
            raise D2LTerminologyDeltaError("added delta must start at revision 1")
    else:
        before = _validate_entry(row["before"], "memory_delta.before")
        revision_before = _integer(row["revision_before"], "revision_before", minimum=1)
        if revision_after != revision_before + 1:
            raise D2LTerminologyDeltaError("updated delta revision is not monotonic")
        if canonical_sha256(before) != _string(row["record_hash_before"], "record_hash_before").upper():
            raise D2LTerminologyDeltaError("memory_delta before hash drift")
        if row["operation"] == "reinforced" and _content_projection(before) != _content_projection(after):
            raise D2LTerminologyDeltaError("reinforced delta changed semantic content")
        if row["operation"] == "revised" and _content_projection(before) == _content_projection(after):
            raise D2LTerminologyDeltaError("revised delta did not change semantic content")
    _mapping(row["evidence_delta"], "memory_delta.evidence_delta")
    _string_list(row["source_refs"], "memory_delta.source_refs")
    receipt = _mapping(row["commit_receipt"], "memory_delta.commit_receipt")
    if set(receipt) != {"receipt_id", "state_generation"}:
        raise D2LTerminologyDeltaError("memory_delta commit_receipt keys mismatch")
    _string(receipt["receipt_id"], "commit_receipt.receipt_id")
    _integer(receipt["state_generation"], "commit_receipt.state_generation", minimum=1)
    return row


def validate_memory_delta_batch(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(value, "memory_delta_batch")
    expected = {
        "schema",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "state_generation",
        "commit_receipt_id",
        "deltas",
        "counts",
        "batch_sha256",
    }
    if set(row) != expected or row["schema"] != MEMORY_DELTA_BATCH_SCHEMA:
        raise D2LTerminologyDeltaError("memory delta batch shape is invalid")
    for key in ("workflow_run_id", "component_run_id", "commit_receipt_id"):
        _string(row[key], f"memory_delta_batch.{key}")
    _integer(row["component_attempt_id"], "component_attempt_id", minimum=1)
    generation = _integer(row["state_generation"], "state_generation", minimum=1)
    if not isinstance(row["deltas"], list):
        raise D2LTerminologyDeltaError("memory_delta_batch.deltas must be an array")
    deltas = [validate_memory_delta(item) for item in row["deltas"]]
    ids = [item["delta_id"] for item in deltas]
    records = [item["record_id"] for item in deltas]
    if len(ids) != len(set(ids)) or len(records) != len(set(records)):
        raise D2LTerminologyDeltaError("memory delta batch identities must be unique")
    if any(
        item["commit_receipt"]["receipt_id"] != row["commit_receipt_id"]
        or item["commit_receipt"]["state_generation"] != generation
        for item in deltas
    ):
        raise D2LTerminologyDeltaError("memory delta receipt lineage mismatch")
    counts = _mapping(row["counts"], "memory_delta_batch.counts")
    if set(counts) != {"added", "reinforced", "revised", "total"}:
        raise D2LTerminologyDeltaError("memory delta counts shape is invalid")
    actual = {
        operation: sum(item["operation"] == operation for item in deltas)
        for operation in ("added", "reinforced", "revised")
    }
    actual["total"] = len(deltas)
    if counts != actual:
        raise D2LTerminologyDeltaError("memory delta counts mismatch")
    if canonical_sha256(_batch_payload(row)) != _string(row["batch_sha256"], "batch_sha256").upper():
        raise D2LTerminologyDeltaError("memory delta batch hash drift")
    return row


def validate_commit_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(value, "commit_receipt")
    expected = {
        "schema",
        "receipt_id",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "stage_id",
        "commit_scope",
        "state_generation",
        "previous_state_sha256",
        "committed_state_sha256",
        "memory_delta_batch_sha256",
        "source_refs",
        "created_at",
        "receipt_sha256",
    }
    if set(row) != expected or row["schema"] != COMMIT_RECEIPT_SCHEMA:
        raise D2LTerminologyDeltaError("commit receipt shape is invalid")
    for key in (
        "receipt_id",
        "workflow_run_id",
        "component_run_id",
        "stage_id",
        "commit_scope",
        "committed_state_sha256",
        "memory_delta_batch_sha256",
        "created_at",
    ):
        _string(row[key], f"commit_receipt.{key}")
    _integer(row["component_attempt_id"], "component_attempt_id", minimum=1)
    _integer(row["state_generation"], "state_generation", minimum=1)
    if row["previous_state_sha256"] is not None:
        _string(row["previous_state_sha256"], "previous_state_sha256")
    _string_list(row["source_refs"], "commit_receipt.source_refs")
    if canonical_sha256(_receipt_payload(row)) != _string(row["receipt_sha256"], "receipt_sha256").upper():
        raise D2LTerminologyDeltaError("commit receipt hash drift")
    return row


def _write_once(path: Path, value: Mapping[str, Any]) -> None:
    encoded = canonical_json_bytes(value)
    if path.exists():
        if path.read_bytes() != encoded:
            raise D2LTerminologyDeltaError(f"immutable artifact already differs: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


def _previous_records(previous: Mapping[str, Any] | None) -> tuple[int, str | None, dict[str, dict[str, Any]]]:
    if previous is None:
        return 0, None, {}
    state = validate_sealed_glossary(previous)
    return (
        int(state["state_generation"]),
        str(state["state_sha256"]),
        {str(item["record_id"]): dict(item) for item in state["records"]},
    )


def commit_glossary_draft(
    *,
    draft: Mapping[str, Any],
    output_root: str | Path,
    workflow_run_id: str,
    component_run_id: str,
    component_attempt_id: int,
    stage_id: str,
    source_refs: Sequence[str],
    created_at: str,
    previous_glossary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    draft_row = _validate_draft(draft)
    previous_generation, previous_sha, previous_records = _previous_records(previous_glossary)
    if previous_glossary is not None:
        if previous_glossary["workflow_run_id"] != workflow_run_id:
            raise D2LTerminologyDeltaError("previous glossary belongs to another workflow")
        if previous_glossary["component_run_id"] != component_run_id:
            raise D2LTerminologyDeltaError("previous glossary belongs to another component run")
        if previous_glossary["component_attempt_id"] > component_attempt_id:
            raise D2LTerminologyDeltaError("previous glossary belongs to a future component attempt")
    generation = previous_generation + 1
    source_refs_row = _string_list(list(source_refs), "source_refs")
    _string(workflow_run_id, "workflow_run_id")
    _string(component_run_id, "component_run_id")
    _integer(component_attempt_id, "component_attempt_id", minimum=1)
    _string(stage_id, "stage_id")
    _string(created_at, "created_at")

    current_ids = {entry["entry_id"] for entry in draft_row["ready_entries"]}
    removed_ids = sorted(set(previous_records) - current_ids)
    if removed_ids:
        raise D2LTerminologyDeltaError(
            "sealed glossary cannot silently remove committed records: " + ", ".join(removed_ids)
        )

    records: list[dict[str, Any]] = []
    changes: list[tuple[str, dict[str, Any] | None, dict[str, Any], int, int]] = []
    for entry in sorted(draft_row["ready_entries"], key=lambda item: item["entry_id"]):
        record_id = entry["entry_id"]
        before_record = previous_records.get(record_id)
        before_entry = None if before_record is None else dict(before_record["value"])
        if before_record is None:
            revision = 1
            operation = "added"
        elif canonical_sha256(before_entry) == canonical_sha256(entry):
            revision = int(before_record["revision"])
            operation = "unchanged"
        else:
            before_blocks = set(before_entry["evidence_block_ids"])
            after_blocks = set(entry["evidence_block_ids"])
            before_candidates = set(before_entry["source_member_candidate_ids"])
            after_candidates = set(entry["source_member_candidate_ids"])
            if not before_blocks.issubset(after_blocks) or not before_candidates.issubset(
                after_candidates
            ):
                raise D2LTerminologyDeltaError(
                    f"committed evidence cannot be silently removed: {record_id}"
                )
            if before_entry["evidence_complete"] and not entry["evidence_complete"]:
                raise D2LTerminologyDeltaError(
                    f"committed evidence_complete cannot regress: {record_id}"
                )
            revision = int(before_record["revision"]) + 1
            operation = (
                "reinforced"
                if _content_projection(before_entry) == _content_projection(entry)
                else "revised"
            )
        record_hash = canonical_sha256(entry)
        records.append(
            {
                "record_id": record_id,
                "revision": revision,
                "record_hash": record_hash,
                "lifecycle": "committed",
                "value": entry,
            }
        )
        if operation != "unchanged":
            changes.append(
                (
                    operation,
                    before_entry,
                    entry,
                    0 if before_record is None else int(before_record["revision"]),
                    revision,
                )
            )

    glossary = {
        "schema": SEALED_GLOSSARY_SCHEMA,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "component_attempt_id": component_attempt_id,
        "state_generation": generation,
        "commit_scope": "component_run",
        "source_draft_sha256": draft_row["draft_sha256"],
        "previous_state_sha256": previous_sha,
        "ready_record_count": len(records),
        "pending_record_count": len(draft_row["pending_entries"]),
        "records": records,
    }
    glossary["state_sha256"] = canonical_sha256(glossary)
    validate_sealed_glossary(glossary)

    receipt_id = "glossary_receipt_" + canonical_sha256(
        {
            "workflow_run_id": workflow_run_id,
            "component_run_id": component_run_id,
            "component_attempt_id": component_attempt_id,
            "state_generation": generation,
            "state_sha256": glossary["state_sha256"],
        }
    )[:24].lower()
    deltas: list[dict[str, Any]] = []
    for operation, before, after, revision_before, revision_after in changes:
        before_hash = None if before is None else canonical_sha256(before)
        after_hash = canonical_sha256(after)
        delta_material = {
            "record_id": after["entry_id"],
            "operation": operation,
            "revision_before": None if before is None else revision_before,
            "revision_after": revision_after,
            "record_hash_before": before_hash,
            "record_hash_after": after_hash,
            "receipt_id": receipt_id,
        }
        delta = {
            "contract": MEMORY_DELTA_CONTRACT,
            "domain": "terminology",
            "collection": "term",
            "operation": operation,
            "lifecycle": "committed",
            "delta_id": "term_delta_" + canonical_sha256(delta_material)[:24].lower(),
            "record_id": after["entry_id"],
            "label": after["canonical_source"],
            "revision_before": None if before is None else revision_before,
            "revision_after": revision_after,
            "record_hash_before": before_hash,
            "record_hash_after": after_hash,
            "before": before,
            "after": after,
            "evidence_delta": _evidence_delta(before, after),
            "reason_code": {
                "added": "initial_or_new_term_commit",
                "reinforced": "evidence_reinforced",
                "revised": "term_record_revised",
            }[operation],
            "source_refs": source_refs_row,
            "commit_receipt": {
                "receipt_id": receipt_id,
                "state_generation": generation,
            },
        }
        validate_memory_delta(delta)
        deltas.append(delta)
    counts = {
        operation: sum(item["operation"] == operation for item in deltas)
        for operation in ("added", "reinforced", "revised")
    }
    counts["total"] = len(deltas)
    batch = {
        "schema": MEMORY_DELTA_BATCH_SCHEMA,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "component_attempt_id": component_attempt_id,
        "state_generation": generation,
        "commit_receipt_id": receipt_id,
        "deltas": deltas,
        "counts": counts,
    }
    batch["batch_sha256"] = canonical_sha256(batch)
    validate_memory_delta_batch(batch)
    receipt = {
        "schema": COMMIT_RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "component_attempt_id": component_attempt_id,
        "stage_id": stage_id,
        "commit_scope": "component_run",
        "state_generation": generation,
        "previous_state_sha256": previous_sha,
        "committed_state_sha256": glossary["state_sha256"],
        "memory_delta_batch_sha256": batch["batch_sha256"],
        "source_refs": source_refs_row,
        "created_at": created_at,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    validate_commit_receipt(receipt)

    root = Path(output_root)
    glossary_path = root / "sealed_glossary.json"
    delta_path = root / "memory_delta_v1.json"
    receipt_path = root / "commit_receipt.json"
    _write_once(glossary_path, glossary)
    _write_once(delta_path, batch)
    _write_once(receipt_path, receipt)
    package = {
        "schema": COMMIT_PACKAGE_SCHEMA,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "component_attempt_id": component_attempt_id,
        "state_generation": generation,
        "artifacts": [
            {
                "artifact_ref": "sealed_glossary",
                "artifact_kind": "d2l_sealed_glossary",
                "schema_version": SEALED_GLOSSARY_SCHEMA,
                "relative_path": "sealed_glossary.json",
                "sha256": file_sha256(glossary_path),
                "sha256_kind": "physical",
            },
            {
                "artifact_ref": "terminology_memory_delta",
                "artifact_kind": "terminology_memory_delta",
                "schema_version": MEMORY_DELTA_BATCH_SCHEMA,
                "relative_path": "memory_delta_v1.json",
                "sha256": file_sha256(delta_path),
                "sha256_kind": "physical",
            },
            {
                "artifact_ref": "glossary_commit_receipt",
                "artifact_kind": "glossary_commit_receipt",
                "schema_version": COMMIT_RECEIPT_SCHEMA,
                "relative_path": "commit_receipt.json",
                "sha256": file_sha256(receipt_path),
                "sha256_kind": "physical",
            },
        ],
    }
    package["package_sha256"] = canonical_sha256(package)
    _write_once(root / "artifact_manifest.json", package)
    return {
        "sealed_glossary": glossary,
        "memory_delta_batch": batch,
        "commit_receipt": receipt,
        "artifact_manifest": package,
    }


__all__ = [
    "COMMIT_PACKAGE_SCHEMA",
    "COMMIT_RECEIPT_SCHEMA",
    "D2LTerminologyDeltaError",
    "MEMORY_DELTA_BATCH_SCHEMA",
    "MEMORY_DELTA_CONTRACT",
    "SEALED_GLOSSARY_SCHEMA",
    "commit_glossary_draft",
    "validate_commit_receipt",
    "validate_memory_delta",
    "validate_memory_delta_batch",
    "validate_sealed_glossary",
]
