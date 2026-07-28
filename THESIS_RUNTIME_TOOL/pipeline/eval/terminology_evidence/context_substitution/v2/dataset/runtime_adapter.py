from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.input import (
    INPUT_SCHEMA_ID,
    INPUT_SCHEMA_VERSION,
    seal_context_substitution_input,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.provenance import (
    SOURCE_LOCATOR_KIND,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.contract import (
    CANDIDATE_INSTANCES_FILE,
    CANDIDATE_SLOTS_FILE,
    CONTEXTS_FILE,
    FREEZE_SCHEMA_ID,
    FREEZE_SCHEMA_VERSION,
    MANIFEST_FILE,
    TERM_SENSES_FILE,
    read_jsonl,
    validate_freeze_bundle,
)


@dataclass(frozen=True)
class FreezeCandidatePolicy:
    slot_roles: tuple[str, ...] = (
        "canonical",
        "alternative",
        "pending",
    )

    def __post_init__(self) -> None:
        allowed = {"canonical", "alternative", "rejected", "pending"}
        if not self.slot_roles or self.slot_roles[0] != "canonical":
            raise ValueError("candidate policy must begin with canonical")
        if any(role not in allowed for role in self.slot_roles):
            raise ValueError("candidate policy contains an unknown role")
        if self.slot_roles.count("canonical") != 1:
            raise ValueError("candidate policy must contain one canonical role")


def freeze_to_context_substitution_input(
    freeze_dir: Path,
    candidate_policy: FreezeCandidatePolicy | Sequence[str] = (
        "canonical",
        "alternative",
        "pending",
    ),
    *,
    parent_v3_source: Path | None = None,
    source_split: str | None = None,
    expected_zip_sha256: str | None = None,
    expected_parent_zip_sha256: str | None = None,
    review_artifact: Path | None = None,
) -> dict[str, Any]:
    schema_id = _peek_dataset_schema(freeze_dir)
    if schema_id in {
        "D2LContextSupportSetValidationReadyV3",
        "D2LCSTDevelopmentOnlyPilotV1_1",
    }:
        from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.reviewed_support import (
            ReviewedSupportCandidatePolicy,
            reviewed_support_to_context_substitution_input,
        )

        roles = (
            candidate_policy.slot_roles
            if isinstance(candidate_policy, FreezeCandidatePolicy)
            else tuple(candidate_policy)
        )
        adapted = reviewed_support_to_context_substitution_input(
            freeze_dir,
            parent_v3_source=parent_v3_source,
            candidate_policy=ReviewedSupportCandidatePolicy(tuple(roles)),
            source_split=source_split,
            expected_zip_sha256=expected_zip_sha256,
            expected_parent_zip_sha256=expected_parent_zip_sha256,
            review_artifact=review_artifact,
        )
        return adapted["input"]

    freeze_dir = freeze_dir.resolve()
    validation = validate_freeze_bundle(freeze_dir)
    if not validation["ready_for_context_selection"]:
        raise ContractValidationError(
            "freeze_not_ready",
            "$.freeze",
            "candidate slots and part-of-speech must be complete",
        )
    policy = (
        candidate_policy
        if isinstance(candidate_policy, FreezeCandidatePolicy)
        else FreezeCandidatePolicy(tuple(candidate_policy))
    )
    manifest = _load_json(freeze_dir / MANIFEST_FILE)
    requested = manifest["requested_cardinality"]
    candidate_count = int(requested["candidates_per_sense"])
    if len(policy.slot_roles) != candidate_count:
        raise ContractValidationError(
            "candidate_policy",
            "$.candidate_policy.slot_roles",
            f"expected {candidate_count} roles, found {len(policy.slot_roles)}",
        )

    term_rows = read_jsonl(freeze_dir / TERM_SENSES_FILE)
    context_rows = read_jsonl(freeze_dir / CONTEXTS_FILE)
    slot_rows = read_jsonl(freeze_dir / CANDIDATE_SLOTS_FILE)
    candidate_rows = read_jsonl(freeze_dir / CANDIDATE_INSTANCES_FILE)
    document_binding = manifest["source_artifacts"]["document"]
    document = _load_json(Path(document_binding["ref"]))
    document_id = str(document.get("doc_id") or document.get("document_id") or "")
    if not document_id:
        raise ContractValidationError(
            "document_id", "$.source_artifacts.document", "document ID is missing"
        )
    blocks = _document_blocks(document)
    contexts_by_key = _group_rows(context_rows)
    slots_by_key = _group_rows(slot_rows)
    candidate_by_slot = _candidate_by_slot(candidate_rows)

    terms: list[dict[str, Any]] = []
    for term in term_rows:
        key = (str(term["term_id"]), str(term["sense_id"]))
        contexts = contexts_by_key.get(key, [])
        declared_ids = [
            *term.get("primary_context_ids", []),
            *term.get("backup_context_ids", []),
            *term.get("contrastive_context_ids", []),
        ]
        contexts_by_id = {str(row["context_id"]): row for row in contexts}
        if set(declared_ids) != set(contexts_by_id):
            raise ContractValidationError(
                "context_binding",
                f"$.terms[{key[0]}].contexts",
                "declared context IDs differ from the freeze rows",
            )
        runtime_contexts = [
            _runtime_context(
                contexts_by_id[str(context_id)],
                document_id=document_id,
                blocks=blocks,
            )
            for context_id in declared_ids
        ]
        slots = sorted(
            slots_by_key.get(key, []), key=lambda row: int(row["slot_number"])
        )
        targets = []
        for slot, role in zip(slots, policy.slot_roles, strict=True):
            instance = candidate_by_slot.get(str(slot["candidate_slot_id"]))
            if slot.get("status") != "RECORDED" or instance is None:
                raise ContractValidationError(
                    "candidate_binding",
                    f"$.terms[{key[0]}].candidate_targets",
                    "every selected candidate slot must have one recorded instance",
                )
            targets.append(
                {
                    "candidate_target_id": str(instance["candidate_instance_id"]),
                    "role": role,
                    "target_vi": str(instance["candidate_target_vi"]),
                    "applicability": _nullable_text(instance.get("applicability")),
                    "candidate_generation": _candidate_generation(instance),
                }
            )
        definition = str(term.get("definition") or "")
        if not definition.strip():
            raise ContractValidationError(
                "sense_definition",
                f"$.terms[{key[0]}].definition",
                "a nonempty source definition is required",
            )
        terms.append(
            {
                "term_id": key[0],
                "source_term": str(term["source_term"]),
                "sense_id": key[1],
                "scope_id": str(term["scope_id"]),
                "sense_contract": {
                    "definition_en": definition,
                    "definition_source": "support_set_freeze_term_sense",
                    "definition_provenance": [
                        f"freeze_term_sense:{term['term_sense_sha256']}"
                    ],
                    "definition_review_status": _definition_status(
                        term.get("definition_status")
                    ),
                    "sense_inventory_version": str(
                        term.get("dataset_version") or FREEZE_SCHEMA_VERSION
                    ),
                },
                "part_of_speech": str(term["part_of_speech"]),
                "source_occurrences": [
                    str(context_id) for context_id in declared_ids
                ],
                "contexts": runtime_contexts,
                "candidate_targets": targets,
            }
        )

    source_artifacts = {
        name: {
            "ref": str(manifest["source_artifacts"][name]["ref"]),
            "physical_sha256": str(
                manifest["source_artifacts"][name]["physical_sha256"]
            ),
        }
        for name in ("candidate_index", "glossary", "document")
    }
    return seal_context_substitution_input(
        {
            "schema_id": INPUT_SCHEMA_ID,
            "schema_version": INPUT_SCHEMA_VERSION,
            "input_origin": {
                "kind": "SUPPORT_SET_FREEZE_V1",
                "source_schema_id": FREEZE_SCHEMA_ID,
                "source_schema_version": FREEZE_SCHEMA_VERSION,
                "source_sha256": str(manifest["manifest_sha256"]),
            },
            "source_artifacts": source_artifacts,
            "terms": terms,
            "integrity": {"input_sha256": "0" * 64},
        }
    )


def _runtime_context(
    row: Mapping[str, Any],
    *,
    document_id: str,
    blocks: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    raw_provenance = row["provenance"]
    block_id = str(raw_provenance["block_id"])
    block = blocks.get(block_id)
    if block is None:
        raise ContractValidationError(
            "source_block", "$.contexts", f"source block not found: {block_id}"
        )
    return {
        "context_id": str(row["context_id"]),
        "chapter_id": str(raw_provenance["chapter_id"]),
        "block_id": block_id,
        "block_type": block["block_type"],
        "source_text": str(row["source_text"]),
        "source_text_sha256": str(row["content_sha256"]),
        "source_provenance": {
            "document_id": document_id,
            "chapter_id": str(raw_provenance["chapter_id"]),
            "block_id": block_id,
            "sentence_id": str(raw_provenance["sentence_id"]),
            "source_start": int(raw_provenance["source_start"]),
            "source_end": int(raw_provenance["source_end"]),
            "source_locator_kind": SOURCE_LOCATOR_KIND,
            "source_hash": str(row["content_sha256"]),
        },
        "reviewed_selection": None,
    }


def _candidate_generation(instance: Mapping[str, Any]) -> dict[str, Any]:
    evidence = instance.get("formation_provenance")
    rows = evidence if isinstance(evidence, list) else []
    generator_model = _first_evidence_value(rows, ("generator_model", "model_id"))
    prompt_version = _first_evidence_value(rows, ("prompt_version",))
    run_id = _first_evidence_value(rows, ("run_id", "workflow_run_id"))
    status = (
        "RECORDED"
        if any((generator_model, prompt_version, run_id))
        else "UNAVAILABLE_IN_SEALED_ARTIFACT"
    )
    return {
        "generator_model": generator_model,
        "prompt_version": prompt_version,
        "run_id": run_id,
        "recording_status": status,
        "candidate_version": None,
        "candidate_slot_id": None,
        "candidate_slot_status": "UNAVAILABLE_IN_LEGACY_ARTIFACT",
        "formation_method": None,
    }


def _peek_dataset_schema(source: Path) -> str | None:
    source = Path(source)
    try:
        if source.is_dir():
            manifest_path = source / MANIFEST_FILE
            if not manifest_path.is_file():
                return None
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        elif source.is_file() and source.suffix.casefold() == ".zip":
            with zipfile.ZipFile(source) as archive:
                roots = [
                    name
                    for name in archive.namelist()
                    if name.rstrip("/") == MANIFEST_FILE
                ]
                if len(roots) != 1:
                    return None
                value = json.loads(archive.read(roots[0]).decode("utf-8"))
        else:
            return None
    except (OSError, ValueError, zipfile.BadZipFile):
        return None
    return str(value.get("schema_id")) if isinstance(value, Mapping) else None


def _first_evidence_value(
    rows: Sequence[Mapping[str, Any]], keys: Sequence[str]
) -> str | None:
    for row in rows:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _definition_status(value: Any) -> str:
    rendered = str(value or "").strip().upper()
    if rendered.startswith("VERIFIED"):
        return "VERIFIED"
    if rendered.startswith("INVALID"):
        return "INVALID"
    return "UNVERIFIED"


def _candidate_by_slot(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        slot_id = str(row["candidate_slot_id"])
        if slot_id in result:
            raise ContractValidationError(
                "candidate_binding",
                "$.candidate_instances",
                f"multiple instances bind slot {slot_id}",
            )
        result[slot_id] = row
    return result


def _group_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    result: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (str(row["term_id"]), str(row["sense_id"]))
        result.setdefault(key, []).append(row)
    return result


def _document_blocks(document: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for chapter in document.get("chapters", []):
        if not isinstance(chapter, Mapping):
            continue
        for block in chapter.get("blocks", []):
            if not isinstance(block, Mapping) or "block_id" not in block:
                continue
            result[str(block["block_id"])] = {
                "block_type": str(block.get("block_type") or "unknown")
            }
    return result


def _nullable_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractValidationError("type", str(path), "expected an object")
    return value

