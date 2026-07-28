from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.input import (
    INPUT_SCHEMA_ID,
    INPUT_SCHEMA_VERSION,
    seal_context_substitution_input,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.provenance import (
    SOURCE_LOCATOR_KIND,
)
ADAPTER_SCHEMA_ID = "D2LContextSubstitutionReviewedSupportAdapterV1"
ADAPTER_SCHEMA_VERSION = "1.0.0"
RECEIPT_SCHEMA_ID = "D2LContextSubstitutionAdapterReceiptV1"
RECEIPT_SCHEMA_VERSION = "1.0.0"

V3_SCHEMA_ID = "D2LContextSupportSetValidationReadyV3"
V3_SCHEMA_VERSION = "3.0.0"
V3_POLICY_ID = "d2l_context_support_validation_ready_v3"
V3_STATUS = "VALIDATION_READY_HUMAN_REVIEW_REQUIRED"
PILOT_SCHEMA_ID = "D2LCSTDevelopmentOnlyPilotV1_1"
PILOT_SCHEMA_VERSION = "1.1.0"
PILOT_STATUS = "DEVELOPMENT_PILOT_HUMAN_REVIEW_REQUIRED"

TERM_FILE = "term_senses.jsonl"
CONTEXT_FILE = "contexts.jsonl"
CANDIDATE_FILE = "candidate_instances.jsonl"
SLOT_FILE = "candidate_slots.jsonl"
MANIFEST_FILE = "manifest.json"


@dataclass(frozen=True)
class ReviewedSupportCandidatePolicy:
    slot_roles: tuple[str, ...] = (
        "canonical",
        "alternative",
        "pending",
    )

    def __post_init__(self) -> None:
        allowed = {"canonical", "alternative", "rejected", "pending"}
        if len(self.slot_roles) != 3:
            raise ValueError("reviewed-support candidate policy requires three roles")
        if self.slot_roles[0] != "canonical":
            raise ValueError("candidate policy must begin with canonical")
        if len(set(self.slot_roles)) != len(self.slot_roles):
            raise ValueError("candidate policy roles must be unique")
        if any(role not in allowed for role in self.slot_roles):
            raise ValueError("candidate policy contains an unknown role")


@dataclass(frozen=True)
class ReviewedSupportBundle:
    manifest: dict[str, Any]
    manifest_file_sha256: str
    source_zip_sha256: str | None
    terms: tuple[dict[str, Any], ...]
    contexts: tuple[dict[str, Any], ...]
    candidates: tuple[dict[str, Any], ...]
    slots: tuple[dict[str, Any], ...]
    file_hashes: dict[str, str]

    @property
    def schema_id(self) -> str:
        return str(self.manifest["schema_id"])

    @property
    def manifest_sha256(self) -> str:
        return str(self.manifest["manifest_sha256"])


def validate_reviewed_support_bundle(
    source: Path,
    *,
    expected_zip_sha256: str | None = None,
    parent_v3_source: Path | None = None,
    expected_parent_zip_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate real V3/pilot bytes without dereferencing workstation paths."""

    with _materialized_bundle(
        source, expected_zip_sha256=expected_zip_sha256
    ) as (root, source_zip_sha256):
        bundle = _load_bundle(root, source_zip_sha256=source_zip_sha256)
    parent = None
    if bundle.schema_id == PILOT_SCHEMA_ID:
        if parent_v3_source is None:
            _fail(
                "parent_dataset",
                "$.parent_v3_source",
                "pilot V1.1 requires the exact V3 parent bundle",
            )
        with _materialized_bundle(
            parent_v3_source,
            expected_zip_sha256=expected_parent_zip_sha256,
        ) as (parent_root, parent_zip_sha256):
            parent = _load_bundle(
                parent_root, source_zip_sha256=parent_zip_sha256
            )
        _validate_pilot_parent(bundle, parent)
    return _validation_summary(bundle, parent=parent)


def reviewed_support_to_context_substitution_input(
    source: Path,
    *,
    parent_v3_source: Path | None = None,
    candidate_policy: ReviewedSupportCandidatePolicy | Sequence[str] = (
        "canonical",
        "alternative",
        "pending",
    ),
    source_split: str | None = None,
    expected_zip_sha256: str | None = None,
    expected_parent_zip_sha256: str | None = None,
    review_artifact: Path | None = None,
) -> dict[str, Any]:
    """Adapt V3 or pilot V1.1 into strict CST input plus a zero-API receipt."""

    policy = (
        candidate_policy
        if isinstance(candidate_policy, ReviewedSupportCandidatePolicy)
        else ReviewedSupportCandidatePolicy(tuple(candidate_policy))
    )
    with _materialized_bundle(
        source, expected_zip_sha256=expected_zip_sha256
    ) as (root, source_zip_sha256):
        bundle = _load_bundle(root, source_zip_sha256=source_zip_sha256)

    parent: ReviewedSupportBundle | None = None
    if bundle.schema_id == PILOT_SCHEMA_ID:
        if parent_v3_source is None:
            _fail(
                "parent_dataset",
                "$.parent_v3_source",
                "pilot V1.1 requires the exact V3 parent bundle",
            )
        with _materialized_bundle(
            parent_v3_source,
            expected_zip_sha256=expected_parent_zip_sha256,
        ) as (parent_root, parent_zip_sha256):
            parent = _load_bundle(
                parent_root, source_zip_sha256=parent_zip_sha256
            )
        _validate_pilot_parent(bundle, parent)
        if source_split not in (None, "development"):
            _fail(
                "split",
                "$.source_split",
                "pilot V1.1 contains development rows only",
            )
        source_split = "development"
    else:
        if source_split not in {"development", "validation", "test"}:
            _fail(
                "split",
                "$.source_split",
                "V3 adaptation requires an explicit development/validation/test split",
            )

    frozen = None
    if review_artifact is not None:
        if bundle.schema_id != PILOT_SCHEMA_ID:
            _fail(
                "review_artifact",
                "$.review_artifact",
                "frozen human review is defined for pilot V1.1 only",
            )
        from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.reviewed_selection import (
            load_frozen_review_selection,
        )

        frozen = load_frozen_review_selection(
            review_artifact,
            source_pilot_manifest_sha256=bundle.manifest_sha256,
            pilot_terms=bundle.terms,
            pilot_contexts=bundle.contexts,
            pilot_candidates=bundle.candidates,
        )

    selected_terms = [
        row for row in bundle.terms if str(row.get("split")) == source_split
    ]
    selected_keys = {
        (str(row["term_id"]), str(row["sense_id"])) for row in selected_terms
    }
    selected_contexts = [
        row
        for row in bundle.contexts
        if (str(row["term_id"]), str(row["sense_id"])) in selected_keys
    ]
    selected_candidates = [
        row
        for row in bundle.candidates
        if (str(row["term_id"]), str(row["sense_id"])) in selected_keys
    ]
    slot_source = parent.slots if parent is not None else bundle.slots
    selected_slots = [
        row
        for row in slot_source
        if str(row.get("candidate_instance_id"))
        in {str(candidate["candidate_instance_id"]) for candidate in selected_candidates}
    ]
    payload = _build_input(
        bundle=bundle,
        parent=parent,
        terms=selected_terms,
        contexts=selected_contexts,
        candidates=selected_candidates,
        slots=selected_slots,
        policy=policy,
        frozen=frozen,
    )
    receipt = _build_receipt(
        bundle=bundle,
        parent=parent,
        payload=payload,
        selected_term_count=len(selected_terms),
        selected_candidate_count=len(selected_candidates),
        selected_context_count=len(selected_contexts),
        frozen=frozen,
    )
    return {"input": payload, "receipt": receipt}


def validate_reviewed_support_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("receipt_type", "$.receipt", "expected an object")
    row = dict(value)
    required = {
        "schema_id",
        "schema_version",
        "agent",
        "adapter_schema_id",
        "adapter_schema_version",
        "source_schema_id",
        "source_schema_version",
        "source_zip_sha256",
        "source_manifest_sha256",
        "source_manifest_file_sha256",
        "parent_dataset_manifest_sha256",
        "effective_sense_contract_sha256",
        "review_artifact_sha256",
        "source_term_sense_count",
        "source_candidate_count",
        "source_context_count",
        "adapted_term_sense_count",
        "adapted_candidate_count",
        "adapted_context_count",
        "input_sha256",
        "mode",
        "provider_call_count",
        "final_glossary_decision",
        "unavailable_fields",
        "receipt_sha256",
    }
    if set(row) != required:
        _fail("receipt_shape", "$.receipt", "unexpected receipt fields")
    exact = {
        "schema_id": RECEIPT_SCHEMA_ID,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "agent": "CONTEXT_SUBSTITUTION_C",
        "adapter_schema_id": ADAPTER_SCHEMA_ID,
        "adapter_schema_version": ADAPTER_SCHEMA_VERSION,
    }
    for key, expected in exact.items():
        if row[key] != expected:
            _fail("receipt_identity", f"$.receipt.{key}", f"expected {expected}")
    for key in (
        "source_manifest_sha256",
        "source_manifest_file_sha256",
        "input_sha256",
        "receipt_sha256",
    ):
        row[key] = _require_sha256(row[key], f"$.receipt.{key}")
    for key in (
        "source_zip_sha256",
        "parent_dataset_manifest_sha256",
        "effective_sense_contract_sha256",
        "review_artifact_sha256",
    ):
        if row[key] is not None:
            row[key] = _require_sha256(row[key], f"$.receipt.{key}")
    for key in (
        "source_term_sense_count",
        "source_candidate_count",
        "source_context_count",
        "adapted_term_sense_count",
        "adapted_candidate_count",
        "adapted_context_count",
    ):
        if isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] < 0:
            _fail("receipt_count", f"$.receipt.{key}", "expected a nonnegative integer")
    if row["provider_call_count"] != 0 or row["final_glossary_decision"] is not None:
        _fail("receipt_authority", "$.receipt", "adapter must remain zero-API and decision-neutral")
    if row["mode"] not in {"DEVELOPMENT_ZERO_API", "FROZEN_HUMAN_REVIEWED_ZERO_API"}:
        _fail("receipt_mode", "$.receipt.mode", "unsupported adapter mode")
    unavailable = row["unavailable_fields"]
    if not isinstance(unavailable, list) or any(not isinstance(item, str) for item in unavailable):
        _fail("receipt_unavailable", "$.receipt.unavailable_fields", "expected a string list")
    if len(unavailable) != len(set(unavailable)):
        _fail("receipt_unavailable", "$.receipt.unavailable_fields", "duplicates are forbidden")
    claimed = row["receipt_sha256"]
    unhashed = dict(row)
    unhashed.pop("receipt_sha256")
    if claimed != _object_hash(unhashed):
        _fail("receipt_hash", "$.receipt.receipt_sha256", "receipt self-hash mismatch")
    return row


def _build_input(
    *,
    bundle: ReviewedSupportBundle,
    parent: ReviewedSupportBundle | None,
    terms: Sequence[Mapping[str, Any]],
    contexts: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    slots: Sequence[Mapping[str, Any]],
    policy: ReviewedSupportCandidatePolicy,
    frozen: Mapping[str, Any] | None,
) -> dict[str, Any]:
    contexts_by_key = _group_by_key(contexts)
    candidates_by_key = _group_by_key(candidates)
    slots_by_candidate = {
        str(row["candidate_instance_id"]): row for row in slots
    }
    if len(slots_by_candidate) != len(slots):
        _fail("candidate_slot", "$.candidate_slots", "duplicate candidate binding")

    frozen_senses = {} if frozen is None else frozen["senses"]
    frozen_contexts = {} if frozen is None else frozen["contexts"]
    normalized_terms: list[dict[str, Any]] = []
    for term in sorted(terms, key=lambda row: (str(row["term_id"]), str(row["sense_id"]))):
        key = (str(term["term_id"]), str(term["sense_id"]))
        term_contexts = contexts_by_key.get(key, [])
        term_candidates = candidates_by_key.get(key, [])
        if not term_contexts or len(term_candidates) != 3:
            _fail(
                "cardinality",
                f"$.terms[{key[0]}]",
                "every adapted sense requires contexts and exactly three candidates",
            )
        targets = []
        ordered_candidates = sorted(
            term_candidates,
            key=lambda row: int(
                slots_by_candidate[str(row["candidate_instance_id"])]["slot_number"]
            ),
        )
        for candidate, role in zip(ordered_candidates, policy.slot_roles, strict=True):
            slot = slots_by_candidate[str(candidate["candidate_instance_id"])]
            targets.append(
                {
                    "candidate_target_id": str(candidate["candidate_instance_id"]),
                    "role": role,
                    "target_vi": str(candidate["candidate_target_vi"]),
                    "applicability": _nullable_text(candidate.get("applicability")),
                    "candidate_generation": _candidate_generation(candidate, slot),
                }
            )
        sense = frozen_senses.get(key[1])
        if frozen is not None and sense is None:
            _fail(
                "review_cover",
                f"$.review.senses[{key[1]}]",
                "frozen sense contract does not cover the selected sense",
            )
        runtime_contexts = [
            _runtime_context(
                row,
                reviewed_selection=(
                    None
                    if frozen is None
                    else frozen_contexts.get(str(row["context_id"]))
                ),
            )
            for row in sorted(
                term_contexts,
                key=lambda row: (
                    _context_role_order(str(row["context_role"])),
                    str(row["context_id"]),
                ),
            )
        ]
        if frozen is not None and any(
            row["reviewed_selection"] is None for row in runtime_contexts
        ):
            _fail(
                "review_cover",
                f"$.review.contexts[{key[1]}]",
                "frozen review does not cover every selected context",
            )
        definition = (
            str(term["definition"])
            if sense is None
            else str(sense["effective_definition_en"])
        )
        part_of_speech = (
            str(term["part_of_speech"])
            if sense is None
            else str(sense["effective_part_of_speech"])
        )
        definition_status = "UNVERIFIED" if sense is None else "VERIFIED"
        definition_source = (
            "validation_ready_v3_model_proposal_pending_human_review"
            if sense is None
            else "frozen_human_reviewed_sense_contract"
        )
        normalized_terms.append(
            {
                "term_id": key[0],
                "source_term": str(term["source_term"]),
                "sense_id": key[1],
                "scope_id": str(term["scope_id"]),
                "sense_contract": {
                    "definition_en": definition,
                    "definition_source": definition_source,
                    "definition_provenance": [
                        f"term_sense:{term['term_sense_sha256']}",
                        *(
                            []
                            if sense is None
                            else [
                                "reviewed_sense_contract:"
                                f"{sense['reviewed_sense_contract_sha256']}"
                            ]
                        ),
                    ],
                    "definition_review_status": definition_status,
                    "sense_inventory_version": (
                        str(term["dataset_version"])
                        if frozen is None
                        else str(frozen["sense_inventory_version"])
                    ),
                },
                "part_of_speech": part_of_speech,
                "source_occurrences": [
                    str(context_id)
                    for context_id in (
                        list(term.get("primary_context_ids", []))
                        + list(term.get("backup_context_ids", []))
                        + list(term.get("contrastive_context_ids", []))
                    )
                ],
                "contexts": runtime_contexts,
                "candidate_targets": targets,
            }
        )

    origin_kind = (
        "FROZEN_HUMAN_REVIEWED_PILOT_V1"
        if frozen is not None
        else (
            "DEVELOPMENT_PILOT_V1_1"
            if bundle.schema_id == PILOT_SCHEMA_ID
            else "VALIDATION_READY_SUPPORT_SET_V3"
        )
    )
    parent_hash = None if parent is None else parent.manifest_sha256
    selection_contract = {
        "selector_mode": (
            "FROZEN_HUMAN_REVIEWED_SELECTION"
            if frozen is not None
            else "MODEL_CLASSIFICATION_DEVELOPMENT"
        ),
        "authority_status": (
            "FROZEN_HUMAN_REVIEWED"
            if frozen is not None
            else "DEVELOPMENT_PENDING_HUMAN_REVIEW"
        ),
        "dataset_manifest_sha256": bundle.manifest_sha256,
        "parent_dataset_manifest_sha256": parent_hash,
        "review_artifact_ref": None if frozen is None else frozen["review_artifact_ref"],
        "review_artifact_sha256": None if frozen is None else frozen["review_artifact_sha256"],
        "effective_sense_contract_ref": (
            None if frozen is None else frozen["effective_sense_contract_ref"]
        ),
        "effective_sense_contract_sha256": (
            None if frozen is None else frozen["effective_sense_contract_sha256"]
        ),
    }
    return seal_context_substitution_input(
        {
            "schema_id": INPUT_SCHEMA_ID,
            "schema_version": INPUT_SCHEMA_VERSION,
            "input_origin": {
                "kind": origin_kind,
                "source_schema_id": bundle.manifest["schema_id"],
                "source_schema_version": bundle.manifest["schema_version"],
                "source_sha256": bundle.manifest_sha256,
            },
            "source_artifacts": _portable_source_artifacts(bundle, terms, contexts),
            "selection_contract": selection_contract,
            "terms": normalized_terms,
            "integrity": {"input_sha256": "0" * 64},
        }
    )


def _runtime_context(
    row: Mapping[str, Any],
    *,
    reviewed_selection: Mapping[str, Any] | None,
) -> dict[str, Any]:
    provenance = row["provenance"]
    if row.get("binding_kind") == "SYNTHETIC_BOUNDARY_PROBE":
        source_provenance = {
            "document_id": "synthetic-contrastive-review-probe",
            "chapter_id": "synthetic-contrastive",
            "block_id": str(row["context_id"]),
            "sentence_id": str(row["context_id"]),
            "source_start": 0,
            "source_end": len(str(row["source_text"])),
            "source_locator_kind": SOURCE_LOCATOR_KIND,
            "source_hash": str(row["content_sha256"]),
        }
    else:
        source_provenance = {
            "document_id": str(provenance["document_id"]),
            "chapter_id": str(provenance["chapter_id"]),
            "block_id": str(provenance["block_id"]),
            "sentence_id": str(provenance["sentence_id"]),
            "source_start": int(provenance["source_start"]),
            "source_end": int(provenance["source_end"]),
            "source_locator_kind": SOURCE_LOCATOR_KIND,
            "source_hash": str(row["content_sha256"]),
        }
    return {
        "context_id": str(row["context_id"]),
        "chapter_id": source_provenance["chapter_id"],
        "block_id": source_provenance["block_id"],
        "block_type": "sentence",
        "source_text": str(row["source_text"]),
        "source_text_sha256": str(row["content_sha256"]),
        "source_provenance": source_provenance,
        "reviewed_selection": (
            None if reviewed_selection is None else dict(reviewed_selection)
        ),
    }


def _candidate_generation(
    candidate: Mapping[str, Any], slot: Mapping[str, Any]
) -> dict[str, Any]:
    evidence = candidate.get("formation_provenance")
    rows = evidence if isinstance(evidence, list) else []
    generator_model = _first_value(rows, ("generator_model", "model_id"))
    prompt_version = _first_value(rows, ("prompt_version",))
    run_id = _first_value(
        rows,
        (
            "run_id",
            "workflow_run_id",
            "audit_sha256",
            "source_artifact_sha256",
        ),
    )
    recording_status = (
        "RECORDED"
        if any((generator_model, prompt_version, run_id))
        else "UNAVAILABLE_IN_SEALED_ARTIFACT"
    )
    return {
        "generator_model": generator_model,
        "prompt_version": prompt_version,
        "run_id": run_id,
        "recording_status": recording_status,
        "candidate_version": str(candidate["candidate_instance_sha256"]),
        "candidate_slot_id": str(candidate["candidate_slot_id"]),
        "candidate_slot_status": str(slot["status"]),
        "formation_method": str(candidate["formation_method"]),
    }


def _portable_source_artifacts(
    bundle: ReviewedSupportBundle,
    terms: Sequence[Mapping[str, Any]],
    contexts: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    glossary_hash = _single_hash(
        [row.get("provenance", {}).get("source_artifact_sha256") for row in terms],
        path="$.term_senses[*].provenance.source_artifact_sha256",
    )
    document_hash = _single_hash(
        [
            row.get("provenance", {}).get("source_artifact_sha256")
            for row in contexts
            if row.get("binding_kind") != "SYNTHETIC_BOUNDARY_PROBE"
        ],
        path="$.contexts[*].provenance.source_artifact_sha256",
    )
    return {
        "candidate_index": {
            "ref": _artifact_uri(bundle, CANDIDATE_FILE),
            "physical_sha256": bundle.file_hashes[CANDIDATE_FILE],
        },
        "glossary": {
            "ref": f"artifact://external/{glossary_hash}/glossary.json",
            "physical_sha256": glossary_hash,
        },
        "document": {
            "ref": f"artifact://external/{document_hash}/document.json",
            "physical_sha256": document_hash,
        },
    }


def _build_receipt(
    *,
    bundle: ReviewedSupportBundle,
    parent: ReviewedSupportBundle | None,
    payload: Mapping[str, Any],
    selected_term_count: int,
    selected_candidate_count: int,
    selected_context_count: int,
    frozen: Mapping[str, Any] | None,
) -> dict[str, Any]:
    receipt = {
        "schema_id": RECEIPT_SCHEMA_ID,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "agent": "CONTEXT_SUBSTITUTION_C",
        "adapter_schema_id": ADAPTER_SCHEMA_ID,
        "adapter_schema_version": ADAPTER_SCHEMA_VERSION,
        "source_schema_id": bundle.schema_id,
        "source_schema_version": bundle.manifest["schema_version"],
        "source_zip_sha256": bundle.source_zip_sha256,
        "source_manifest_sha256": bundle.manifest_sha256,
        "source_manifest_file_sha256": bundle.manifest_file_sha256,
        "parent_dataset_manifest_sha256": (
            None if parent is None else parent.manifest_sha256
        ),
        "effective_sense_contract_sha256": (
            None if frozen is None else frozen["effective_sense_contract_sha256"]
        ),
        "review_artifact_sha256": (
            None if frozen is None else frozen["review_artifact_sha256"]
        ),
        "source_term_sense_count": len(bundle.terms),
        "source_candidate_count": len(bundle.candidates),
        "source_context_count": len(bundle.contexts),
        "adapted_term_sense_count": selected_term_count,
        "adapted_candidate_count": selected_candidate_count,
        "adapted_context_count": selected_context_count,
        "input_sha256": payload["integrity"]["input_sha256"],
        "mode": (
            "FROZEN_HUMAN_REVIEWED_ZERO_API"
            if frozen is not None
            else "DEVELOPMENT_ZERO_API"
        ),
        "provider_call_count": 0,
        "final_glossary_decision": None,
        "unavailable_fields": (
            []
            if frozen is not None
            else [
                "effective_sense_contract_sha256",
                "review_artifact_sha256",
                "human_context_labels",
            ]
        ),
    }
    receipt["receipt_sha256"] = _object_hash(receipt)
    return receipt


def _load_bundle(root: Path, *, source_zip_sha256: str | None) -> ReviewedSupportBundle:
    manifest_path = root / MANIFEST_FILE
    if not manifest_path.is_file():
        _fail("manifest", "$.manifest", "manifest.json is missing")
    manifest = _load_json(manifest_path)
    schema_id = str(manifest.get("schema_id") or "")
    if schema_id == V3_SCHEMA_ID:
        _require_exact_value(manifest, "schema_version", V3_SCHEMA_VERSION)
        _require_exact_value(manifest, "policy_id", V3_POLICY_ID)
        _require_exact_value(manifest, "status", V3_STATUS)
    elif schema_id == PILOT_SCHEMA_ID:
        _require_exact_value(manifest, "schema_version", PILOT_SCHEMA_VERSION)
        _require_exact_value(manifest, "status", PILOT_STATUS)
    else:
        _fail("schema", "$.manifest.schema_id", f"unsupported schema: {schema_id}")
    _verify_self_hash(manifest, "manifest_sha256", path="$.manifest")
    file_hashes = _verify_manifest_files(root, manifest)
    terms = _load_jsonl(root / TERM_FILE)
    contexts = _load_jsonl(root / CONTEXT_FILE)
    candidates = _load_jsonl(root / CANDIDATE_FILE)
    slots = _load_jsonl(root / SLOT_FILE) if schema_id == V3_SCHEMA_ID else []
    _validate_rows(
        schema_id=schema_id,
        terms=terms,
        contexts=contexts,
        candidates=candidates,
        slots=slots,
        manifest=manifest,
    )
    return ReviewedSupportBundle(
        manifest=manifest,
        manifest_file_sha256=_sha256_file(manifest_path),
        source_zip_sha256=source_zip_sha256,
        terms=tuple(terms),
        contexts=tuple(contexts),
        candidates=tuple(candidates),
        slots=tuple(slots),
        file_hashes=file_hashes,
    )


def _validate_rows(
    *,
    schema_id: str,
    terms: Sequence[Mapping[str, Any]],
    contexts: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    slots: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> None:
    expected_counts = (
        (150, 450, 1340)
        if schema_id == V3_SCHEMA_ID
        else (
            int(manifest["sense_count"]),
            int(manifest["candidate_count"]),
            int(manifest["context_count"]),
        )
    )
    actual_counts = (len(terms), len(candidates), len(contexts))
    if actual_counts != expected_counts:
        _fail(
            "cardinality",
            "$.bundle",
            f"expected {expected_counts}, found {actual_counts}",
        )
    if schema_id == V3_SCHEMA_ID and len(slots) != len(candidates):
        _fail("cardinality", "$.candidate_slots", "slot/candidate count mismatch")

    _validate_row_set(
        terms,
        schema_id="D2LContextSupportTermSenseV3",
        schema_version="3.0.0",
        id_field="term_id",
        hash_field="term_sense_sha256",
        path="$.term_senses",
    )
    _validate_row_set(
        contexts,
        schema_id="D2LContextSupportContextV3",
        schema_version="3.0.0",
        id_field="context_id",
        hash_field="context_sha256",
        path="$.contexts",
    )
    _validate_row_set(
        candidates,
        schema_id="D2LContextSupportCandidateInstanceV3",
        schema_version="3.0.0",
        id_field="candidate_instance_id",
        hash_field="candidate_instance_sha256",
        path="$.candidate_instances",
    )
    if slots:
        _validate_row_set(
            slots,
            schema_id="D2LContextSupportCandidateSlotV3",
            schema_version="3.0.0",
            id_field="candidate_slot_id",
            hash_field="candidate_slot_sha256",
            path="$.candidate_slots",
        )

    terms_by_key = {
        (str(row["term_id"]), str(row["sense_id"])): row for row in terms
    }
    if len(terms_by_key) != len(terms):
        _fail("duplicate", "$.term_senses", "duplicate term/sense identity")
    contexts_by_key = _group_by_key(contexts)
    candidates_by_key = _group_by_key(candidates)
    context_ids = {str(row["context_id"]): row for row in contexts}
    if len(context_ids) != len(contexts):
        _fail("duplicate", "$.contexts", "duplicate context_id")

    for index, row in enumerate(contexts):
        key = (str(row.get("term_id")), str(row.get("sense_id")))
        term = terms_by_key.get(key)
        if term is None:
            _fail("foreign_reference", f"$.contexts[{index}]", "foreign term/sense")
        _require_join(
            row,
            term,
            fields=("term_id", "sense_id", "shared_context_set_id"),
            path=f"$.contexts[{index}]",
        )
        _validate_context_row(row, path=f"$.contexts[{index}]")
    for index, row in enumerate(candidates):
        key = (str(row.get("term_id")), str(row.get("sense_id")))
        term = terms_by_key.get(key)
        if term is None:
            _fail(
                "foreign_reference",
                f"$.candidate_instances[{index}]",
                "foreign term/sense",
            )
        _require_join(row, term, path=f"$.candidate_instances[{index}]")
        _require_nonempty(row.get("candidate_slot_id"), f"$.candidate_instances[{index}].candidate_slot_id")
        _require_nonempty(row.get("candidate_target_vi"), f"$.candidate_instances[{index}].candidate_target_vi")
        evidence = row.get("formation_provenance")
        if not isinstance(evidence, list) or not evidence:
            _fail(
                "candidate_provenance",
                f"$.candidate_instances[{index}].formation_provenance",
                "formation provenance is required",
            )

    for key, term in terms_by_key.items():
        declared = [
            *term.get("primary_context_ids", []),
            *term.get("backup_context_ids", []),
            *term.get("contrastive_context_ids", []),
        ]
        if len(declared) != len(set(declared)):
            _fail("duplicate", f"$.term_senses[{key}]", "duplicate context reference")
        actual = {str(row["context_id"]) for row in contexts_by_key.get(key, [])}
        if not set(map(str, declared)).issubset(actual):
            _fail(
                "context_reference",
                f"$.term_senses[{key}]",
                "declared context is missing or belongs to another sense",
            )
        if schema_id == PILOT_SCHEMA_ID and set(map(str, declared)) != actual:
            _fail(
                "context_reference",
                f"$.term_senses[{key}]",
                "pilot must contain exactly its 38 referenced contexts",
            )
        if len(candidates_by_key.get(key, [])) != 3:
            _fail(
                "candidate_reference",
                f"$.term_senses[{key}]",
                "every sense requires exactly three candidates",
            )

    if slots:
        slot_by_id = {str(row["candidate_slot_id"]): row for row in slots}
        if len(slot_by_id) != len(slots):
            _fail("duplicate", "$.candidate_slots", "duplicate candidate_slot_id")
        seen_instances: set[str] = set()
        for index, candidate in enumerate(candidates):
            slot = slot_by_id.get(str(candidate["candidate_slot_id"]))
            if slot is None:
                _fail(
                    "candidate_slot",
                    f"$.candidate_instances[{index}]",
                    "candidate slot is missing",
                )
            _require_join(candidate, slot, path=f"$.candidate_instances[{index}]")
            if str(slot.get("candidate_instance_id")) != str(candidate["candidate_instance_id"]):
                _fail(
                    "candidate_slot",
                    f"$.candidate_instances[{index}]",
                    "slot points to a different candidate instance",
                )
            if slot.get("status") not in {"RECORDED", "MODEL_GENERATED"}:
                _fail(
                    "candidate_slot",
                    f"$.candidate_slots[{index}].status",
                    "unsupported candidate slot status",
                )
            if str(slot.get("formation_method")) != str(candidate.get("formation_method")):
                _fail(
                    "candidate_slot",
                    f"$.candidate_slots[{index}].formation_method",
                    "slot and candidate formation methods differ",
                )
            seen_instances.add(str(candidate["candidate_instance_id"]))
        if len(seen_instances) != len(candidates):
            _fail("candidate_slot", "$.candidate_slots", "candidate binding is not one-to-one")
        for key, term_slots in _group_by_key(slots).items():
            numbers = sorted(int(row["slot_number"]) for row in term_slots)
            if numbers != [1, 2, 3]:
                _fail(
                    "candidate_slot",
                    f"$.candidate_slots[{key}]",
                    "slot numbers must be exactly 1,2,3",
                )

    split_by_leakage: dict[str, set[str]] = {}
    for term in terms:
        group = str(term.get("leakage_group_id") or "")
        split = str(term.get("split") or "")
        if not group or split not in {"development", "validation", "test"}:
            _fail("split", "$.term_senses", "invalid split/leakage binding")
        split_by_leakage.setdefault(group, set()).add(split)
    if any(len(values) != 1 for values in split_by_leakage.values()):
        _fail("split_leakage", "$.term_senses", "sentence group crosses data splits")


def _validate_context_row(row: Mapping[str, Any], *, path: str) -> None:
    text = str(row.get("source_text") or "")
    if not text:
        _fail("source_text", f"{path}.source_text", "source text is empty")
    if str(row.get("content_sha256")) != _sha256_bytes(text.encode("utf-8")):
        _fail("source_hash", f"{path}.content_sha256", "source text hash mismatch")
    provenance = row.get("provenance")
    if not isinstance(provenance, Mapping):
        _fail("provenance", f"{path}.provenance", "expected an object")
    synthetic = row.get("binding_kind") == "SYNTHETIC_BOUNDARY_PROBE"
    if synthetic:
        if row.get("context_role") != "CONTRASTIVE" or row.get("sense_relation") != "CONTRASTIVE":
            _fail("synthetic_context", path, "synthetic probe must be contrastive")
        if row.get("source_match_start_absolute") is not None or row.get("source_match_end_absolute") is not None:
            _fail("synthetic_context", path, "synthetic probe cannot claim physical offsets")
        if not isinstance(row.get("synthetic_contrastive"), Mapping):
            _fail("synthetic_context", path, "synthetic probe contract is missing")
        _require_sha256(provenance.get("audit_sha256"), f"{path}.provenance.audit_sha256")
        match_start = _nonnegative_int(row.get("match_start"), f"{path}.match_start")
        match_end = _nonnegative_int(row.get("match_end"), f"{path}.match_end")
        if match_end <= match_start or match_end > len(text):
            _fail("match_range", path, "context-local match range is invalid")
        matched = unicodedata.normalize("NFC", text[match_start:match_end]).casefold()
        declared = unicodedata.normalize("NFC", str(row.get("matched_surface") or "")).casefold()
        if matched != declared:
            _fail("matched_surface", f"{path}.matched_surface", "surface does not match source slice")
        return
    start = _nonnegative_int(provenance.get("source_start"), f"{path}.provenance.source_start")
    end = _nonnegative_int(provenance.get("source_end"), f"{path}.provenance.source_end")
    if end - start != len(text):
        _fail("source_range", f"{path}.provenance", "enclosing source span does not match text")
    match_start = _nonnegative_int(row.get("match_start"), f"{path}.match_start")
    match_end = _nonnegative_int(row.get("match_end"), f"{path}.match_end")
    if match_end <= match_start or match_end > len(text):
        _fail("match_range", path, "context-local match range is invalid")
    absolute_start = _nonnegative_int(
        row.get("source_match_start_absolute"), f"{path}.source_match_start_absolute"
    )
    absolute_end = _nonnegative_int(
        row.get("source_match_end_absolute"), f"{path}.source_match_end_absolute"
    )
    if absolute_start != start + match_start or absolute_end != start + match_end:
        _fail("match_range", path, "local and absolute offsets disagree")
    matched = unicodedata.normalize("NFC", text[match_start:match_end]).casefold()
    declared = unicodedata.normalize("NFC", str(row.get("matched_surface") or "")).casefold()
    if matched != declared:
        _fail("matched_surface", f"{path}.matched_surface", "surface does not match source slice")
    for field in ("document_id", "chapter_id", "block_id", "sentence_id"):
        _require_nonempty(provenance.get(field), f"{path}.provenance.{field}")
    _require_sha256(
        provenance.get("source_artifact_sha256"),
        f"{path}.provenance.source_artifact_sha256",
    )


def _validate_pilot_parent(
    pilot: ReviewedSupportBundle, parent: ReviewedSupportBundle
) -> None:
    if parent.schema_id != V3_SCHEMA_ID:
        _fail("parent_dataset", "$.parent", "pilot parent must be V3")
    binding = pilot.manifest.get("source_v3")
    if not isinstance(binding, Mapping):
        _fail("parent_dataset", "$.manifest.source_v3", "binding is missing")
    if binding.get("manifest_sha256") != parent.manifest_sha256:
        _fail("parent_dataset", "$.manifest.source_v3", "manifest hash mismatch")
    if binding.get("manifest_file_sha256") != parent.manifest_file_sha256:
        _fail("parent_dataset", "$.manifest.source_v3", "physical manifest hash mismatch")
    for label, child_rows, parent_rows, id_field, hash_field in (
        ("term", pilot.terms, parent.terms, "term_id", "term_sense_sha256"),
        ("context", pilot.contexts, parent.contexts, "context_id", "context_sha256"),
        (
            "candidate",
            pilot.candidates,
            parent.candidates,
            "candidate_instance_id",
            "candidate_instance_sha256",
        ),
    ):
        parent_by_id = {str(row[id_field]): row for row in parent_rows}
        for index, child in enumerate(child_rows):
            expected = parent_by_id.get(str(child[id_field]))
            if expected is None or child.get(hash_field) != expected.get(hash_field):
                _fail(
                    "parent_dataset",
                    f"$.{label}[{index}]",
                    "pilot row is absent from or differs from V3",
                )
            if dict(child) != dict(expected):
                _fail(
                    "parent_dataset",
                    f"$.{label}[{index}]",
                    "pilot row is not byte-semantic identical to V3",
                )
    parent_slots = {str(row["candidate_slot_id"]): row for row in parent.slots}
    for index, candidate in enumerate(pilot.candidates):
        slot = parent_slots.get(str(candidate["candidate_slot_id"]))
        if slot is None or str(slot.get("candidate_instance_id")) != str(
            candidate["candidate_instance_id"]
        ):
            _fail(
                "candidate_slot",
                f"$.pilot.candidate_instances[{index}]",
                "pilot candidate does not resolve to one exact V3 slot",
            )


def _validation_summary(
    bundle: ReviewedSupportBundle,
    *,
    parent: ReviewedSupportBundle | None,
) -> dict[str, Any]:
    statuses = {str(row["status"]) for row in (parent.slots if parent else bundle.slots)}
    return {
        "schema_id": "D2LContextSubstitutionReviewedSupportValidationV1",
        "schema_version": "1.0.0",
        "status": "PASS",
        "source_schema_id": bundle.schema_id,
        "source_manifest_sha256": bundle.manifest_sha256,
        "parent_dataset_manifest_sha256": None if parent is None else parent.manifest_sha256,
        "counts": {
            "term_senses": len(bundle.terms),
            "candidate_instances": len(bundle.candidates),
            "contexts": len(bundle.contexts),
        },
        "candidate_slot_statuses": sorted(statuses),
        "provider_call_count": 0,
        "final_glossary_decision": None,
    }


def _verify_manifest_files(
    root: Path, manifest: Mapping[str, Any]
) -> dict[str, str]:
    bindings = manifest.get("files")
    if not isinstance(bindings, Mapping) or not bindings:
        _fail("manifest_files", "$.manifest.files", "expected nonempty bindings")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for name, raw in bindings.items():
        if not isinstance(raw, Mapping):
            _fail("manifest_files", f"$.manifest.files.{name}", "expected object")
        ref = _safe_relative_ref(raw.get("ref"), path=f"$.manifest.files.{name}.ref")
        folded = ref.casefold()
        if folded in seen:
            _fail("manifest_files", "$.manifest.files", "case-confusable duplicate ref")
        seen.add(folded)
        expected = _require_sha256(raw.get("sha256"), f"$.manifest.files.{name}.sha256")
        file_path = root.joinpath(*PurePosixPath(ref).parts)
        if not file_path.is_file() or _sha256_file(file_path) != expected:
            _fail("manifest_files", f"$.manifest.files.{name}", "file hash mismatch")
        result[str(name)] = expected
    for required in (TERM_FILE, CONTEXT_FILE, CANDIDATE_FILE):
        if required not in result:
            _fail("manifest_files", "$.manifest.files", f"missing {required}")
    if manifest.get("schema_id") == V3_SCHEMA_ID and SLOT_FILE not in result:
        _fail("manifest_files", "$.manifest.files", f"missing {SLOT_FILE}")
    return result


def _validate_row_set(
    rows: Sequence[Mapping[str, Any]],
    *,
    schema_id: str,
    schema_version: str,
    id_field: str,
    hash_field: str,
    path: str,
) -> None:
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if row.get("schema_id") != schema_id or row.get("schema_version") != schema_version:
            _fail("schema", f"{path}[{index}]", "unsupported row schema/version")
        row_id = str(row.get(id_field) or "")
        if not row_id or row_id in seen:
            _fail("duplicate", f"{path}[{index}].{id_field}", "missing or duplicate ID")
        seen.add(row_id)
        _verify_self_hash(row, hash_field, path=f"{path}[{index}]")


def _require_join(
    child: Mapping[str, Any],
    parent: Mapping[str, Any],
    *,
    path: str,
    fields: Sequence[str] = (
        "term_id",
        "sense_id",
        "scope_id",
        "shared_context_set_id",
    ),
) -> None:
    for field in fields:
        if str(child.get(field)) != str(parent.get(field)):
            _fail("join_key", f"{path}.{field}", "join key differs from term sense")


def _group_by_key(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    result: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (str(row["term_id"]), str(row["sense_id"]))
        result.setdefault(key, []).append(row)
    return result


@contextmanager
def _materialized_bundle(
    source: Path, *, expected_zip_sha256: str | None
) -> Iterator[tuple[Path, str | None]]:
    source = source.resolve()
    if source.is_dir():
        if expected_zip_sha256 is not None:
            _fail("zip_hash", "$.source", "ZIP hash cannot be supplied for a directory")
        yield source, None
        return
    if not source.is_file() or source.suffix.casefold() != ".zip":
        _fail("source", "$.source", "expected a bundle directory or ZIP")
    if expected_zip_sha256 is None:
        _fail("zip_hash", "$.source", "ZIP mode requires an explicit SHA-256")
    expected_zip_sha256 = _require_sha256(expected_zip_sha256, "$.source_zip_sha256")
    actual_zip_sha256 = _sha256_file(source)
    if actual_zip_sha256 != expected_zip_sha256:
        _fail("zip_hash", "$.source_zip_sha256", "physical ZIP hash mismatch")
    with tempfile.TemporaryDirectory(prefix="cst-reviewed-support-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(source, "r") as archive:
            names: set[str] = set()
            for info in archive.infolist():
                name = info.filename
                if info.is_dir():
                    continue
                safe = _safe_relative_ref(name, path="$.zip.entries")
                folded = safe.casefold()
                if folded in names:
                    _fail("zip_entry", "$.zip.entries", "duplicate/case-confusable entry")
                names.add(folded)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    _fail("zip_entry", "$.zip.entries", "symbolic links are forbidden")
                destination = root.joinpath(*PurePosixPath(safe).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source_handle, destination.open("wb") as target:
                    while chunk := source_handle.read(1024 * 1024):
                        target.write(chunk)
        manifests = []
        for candidate in root.rglob(MANIFEST_FILE):
            value = _load_json(candidate)
            if value.get("schema_id") in {V3_SCHEMA_ID, PILOT_SCHEMA_ID}:
                manifests.append(candidate)
        if len(manifests) != 1:
            _fail(
                "zip_entry",
                "$.zip",
                "ZIP must contain exactly one supported V3 or Pilot root manifest",
            )
        yield manifests[0].parent, actual_zip_sha256


def _safe_relative_ref(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("path", path, "expected nonempty relative path")
    if "\\" in value or value.startswith(("/", "//")) or ":" in value:
        _fail("path", path, "absolute, drive, UNC, and backslash paths are forbidden")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        _fail("path", path, "path is not canonical repo-relative POSIX form")
    rendered = parsed.as_posix()
    if rendered != value:
        _fail("path", path, "path is not canonical")
    return rendered


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail("json", str(path), f"invalid JSON: {exc.__class__.__name__}")
    if not isinstance(value, dict):
        _fail("json", str(path), "expected an object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        _fail("jsonl", str(path), "file is missing")
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            _fail("jsonl", f"{path}:{line_number}", f"invalid JSON: {exc.msg}")
        if not isinstance(row, dict):
            _fail("jsonl", f"{path}:{line_number}", "expected an object")
        rows.append(row)
    return rows


def _verify_self_hash(row: Mapping[str, Any], hash_field: str, *, path: str) -> None:
    actual = _require_sha256(row.get(hash_field), f"{path}.{hash_field}")
    identity = dict(row)
    identity.pop(hash_field, None)
    expected = _object_hash(identity)
    if actual != expected:
        _fail("self_hash", f"{path}.{hash_field}", "hash mismatch")


def _require_exact_value(row: Mapping[str, Any], field: str, expected: str) -> None:
    if row.get(field) != expected:
        _fail("schema", f"$.manifest.{field}", f"expected {expected}")


def _require_sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        _fail("sha256", path, "invalid SHA-256")
    return value


def _require_nonempty(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("string", path, "expected a nonempty string")
    return value


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("integer", path, "expected a nonnegative integer")
    return value


def _single_hash(values: Sequence[Any], *, path: str) -> str:
    if not values:
        return _sha256_bytes(b"no-physical-source-artifact")
    hashes = {_require_sha256(value, path) for value in values}
    if len(hashes) != 1:
        _fail("source_binding", path, "selected rows use multiple source artifacts")
    return next(iter(hashes))


def _first_value(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> str | None:
    for row in rows:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _context_role_order(role: str) -> int:
    return {"PRIMARY": 0, "BACKUP": 1, "CONTRASTIVE": 2, "UNSELECTED": 3}.get(role, 99)


def _artifact_uri(bundle: ReviewedSupportBundle, filename: str) -> str:
    return f"artifact://{bundle.schema_id}/{bundle.manifest_sha256}/{filename}"


def _nullable_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fail(code: str, path: str, message: str) -> None:
    raise ContractValidationError(code, path, message)


__all__ = [
    "ADAPTER_SCHEMA_ID",
    "ADAPTER_SCHEMA_VERSION",
    "PILOT_SCHEMA_ID",
    "PILOT_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_ID",
    "RECEIPT_SCHEMA_VERSION",
    "ReviewedSupportCandidatePolicy",
    "V3_SCHEMA_ID",
    "V3_SCHEMA_VERSION",
    "reviewed_support_to_context_substitution_input",
    "validate_reviewed_support_bundle",
]
