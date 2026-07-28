from __future__ import annotations

from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_nullable_string,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    verify_payload_hash,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    SENSE_DEFINITION_STATUSES,
    sha256_text,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.provenance import (
    validate_source_artifact_bindings,
    validate_source_provenance,
)


INPUT_SCHEMA_ID = "D2LContextSubstitutionInputV2"
INPUT_SCHEMA_VERSION = "2.2.0"
INPUT_HASH_PATH = ("integrity", "input_sha256")
INPUT_ORIGIN_KINDS = frozenset(
    {
        "LEGACY_TERM_EVIDENCE_INPUT_V1",
        "SUPPORT_SET_FREEZE_V1",
        "VALIDATION_READY_SUPPORT_SET_V3",
        "DEVELOPMENT_PILOT_V1_1",
        "FROZEN_HUMAN_REVIEWED_PILOT_V1",
    }
)
TARGET_ROLES = frozenset(
    {"canonical", "alternative", "rejected", "pending"}
)
SELECTOR_MODES = frozenset(
    {
        "MODEL_CLASSIFICATION_DEVELOPMENT",
        "FROZEN_HUMAN_REVIEWED_SELECTION",
    }
)
SELECTION_AUTHORITY_STATUSES = frozenset(
    {
        "DEVELOPMENT_PENDING_HUMAN_REVIEW",
        "FROZEN_HUMAN_REVIEWED",
    }
)
SOURCE_CANDIDATE_STATUSES = frozenset(
    {
        "RECORDED",
        "MODEL_GENERATED",
        "UNAVAILABLE_IN_LEGACY_ARTIFACT",
    }
)

INPUT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("terms",),
            ("terms", "*", "source_occurrences"),
            ("terms", "*", "sense_contract", "definition_provenance"),
            ("terms", "*", "contexts"),
            ("terms", "*", "candidate_targets"),
        }
    ),
)


def normalize_context_substitution_input(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if payload.get("schema_id") == INPUT_SCHEMA_ID:
        return validate_context_substitution_input(payload)
    return legacy_input_to_context_substitution_input(payload)


def legacy_input_to_context_substitution_input(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    from pipeline.eval.terminology_evidence.legacy_term_evidence.v1 import (
        validate_d2l_term_evidence_input,
    )

    legacy = validate_d2l_term_evidence_input(payload)
    terms = []
    for term in legacy["terms"]:
        candidate_generation = dict(term["candidate_generation"])
        terms.append(
            {
                "term_id": term["term_id"],
                "source_term": term["source_term"],
                "sense_id": term["sense_id"],
                "scope_id": term["scope_id"],
                "sense_contract": dict(term["sense_contract"]),
                "part_of_speech": term["part_of_speech"],
                "source_occurrences": list(term["source_occurrences"]),
                "contexts": [
                    {
                        "context_id": context["block_id"],
                        "chapter_id": context["chapter_id"],
                        "block_id": context["block_id"],
                        "block_type": context["block_type"],
                        "source_text": context["source_text"],
                        "source_text_sha256": context[
                            "source_text_sha256"
                        ],
                        "source_provenance": dict(
                            context["source_provenance"]
                        ),
                        "reviewed_selection": None,
                    }
                    for context in term["contexts"]
                ],
                "candidate_targets": [
                    {
                        **dict(target),
                        "candidate_generation": {
                            **candidate_generation,
                            "candidate_version": None,
                            "candidate_slot_id": None,
                            "candidate_slot_status": (
                                "UNAVAILABLE_IN_LEGACY_ARTIFACT"
                            ),
                            "formation_method": None,
                        },
                    }
                    for target in term["candidate_targets"]
                ],
            }
        )
    return seal_context_substitution_input(
        {
            "schema_id": INPUT_SCHEMA_ID,
            "schema_version": INPUT_SCHEMA_VERSION,
            "input_origin": {
                "kind": "LEGACY_TERM_EVIDENCE_INPUT_V1",
                "source_schema_id": legacy["schema_id"],
                "source_schema_version": legacy["schema_version"],
                "source_sha256": legacy["integrity"]["input_sha256"],
            },
            "source_artifacts": legacy["source_artifacts"],
            "selection_contract": _development_selection_contract(
                dataset_manifest_sha256=legacy["integrity"]["input_sha256"]
            ),
            "terms": terms,
            "integrity": {"input_sha256": "0" * 64},
        }
    )


def seal_context_substitution_input(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _with_compatibility_defaults(payload)
    sealed = seal_payload(
        payload,
        policy=INPUT_POLICY,
        hash_path=INPUT_HASH_PATH,
    )
    return validate_context_substitution_input(sealed)


def validate_context_substitution_input(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "input_origin",
            "source_artifacts",
            "selection_contract",
            "terms",
            "integrity",
        },
        path="$",
    )
    terms = [
        _validate_term(row, path=f"$.terms[{index}]")
        for index, row in enumerate(require_list(root["terms"], path="$.terms"))
    ]
    if not terms:
        raise ContractValidationError("missing_term", "$.terms", "must not be empty")
    require_unique([row["term_id"] for row in terms], path="$.terms[*].term_id")
    origin = _validate_input_origin(root["input_origin"])
    selection_contract = _validate_selection_contract(
        root["selection_contract"],
        origin=origin,
    )
    reviewed_rows = [
        context["reviewed_selection"]
        for term in terms
        for context in term["contexts"]
    ]
    if selection_contract["selector_mode"] == "FROZEN_HUMAN_REVIEWED_SELECTION":
        if any(row is None for row in reviewed_rows):
            raise ContractValidationError(
                "selection_cover",
                "$.terms[*].contexts[*].reviewed_selection",
                "frozen selector requires a reviewed row for every context",
            )
    elif any(row is not None for row in reviewed_rows):
        raise ContractValidationError(
            "selection_authority",
            "$.terms[*].contexts[*].reviewed_selection",
            "development input cannot carry frozen reviewed selections",
        )
    integrity = require_mapping(root["integrity"], path="$.integrity")
    require_exact_keys(
        integrity, required={"input_sha256"}, path="$.integrity"
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {INPUT_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {INPUT_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "input_origin": origin,
        "source_artifacts": validate_source_artifact_bindings(
            root["source_artifacts"], path="$.source_artifacts"
        ),
        "selection_contract": selection_contract,
        "terms": terms,
        "integrity": {
            "input_sha256": require_sha256(
                integrity["input_sha256"], path="$.integrity.input_sha256"
            )
        },
    }
    if not verify_payload_hash(
        normalized, policy=INPUT_POLICY, hash_path=INPUT_HASH_PATH
    ):
        raise ContractValidationError(
            "self_hash", "$.integrity.input_sha256", "input self-hash mismatch"
        )
    return canonicalize(normalized, policy=INPUT_POLICY)


def _validate_input_origin(value: Any) -> dict[str, str]:
    path = "$.input_origin"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "kind",
            "source_schema_id",
            "source_schema_version",
            "source_sha256",
        },
        path=path,
    )
    return {
        "kind": require_enum(row["kind"], INPUT_ORIGIN_KINDS, path=f"{path}.kind"),
        "source_schema_id": require_string(
            row["source_schema_id"], path=f"{path}.source_schema_id"
        ),
        "source_schema_version": require_string(
            row["source_schema_version"], path=f"{path}.source_schema_version"
        ),
        "source_sha256": require_sha256(
            row["source_sha256"], path=f"{path}.source_sha256"
        ),
    }


def _validate_term(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "term_id",
            "source_term",
            "sense_id",
            "scope_id",
            "sense_contract",
            "part_of_speech",
            "source_occurrences",
            "contexts",
            "candidate_targets",
        },
        path=path,
    )
    contexts = [
        _validate_context(child, path=f"{path}.contexts[{index}]")
        for index, child in enumerate(require_list(row["contexts"], path=f"{path}.contexts"))
    ]
    if not contexts:
        raise ContractValidationError(
            "missing_context", f"{path}.contexts", "must not be empty"
        )
    require_unique(
        [context["context_id"] for context in contexts],
        path=f"{path}.contexts[*].context_id",
    )
    targets = [
        _validate_target(child, path=f"{path}.candidate_targets[{index}]")
        for index, child in enumerate(
            require_list(row["candidate_targets"], path=f"{path}.candidate_targets")
        )
    ]
    if not targets or targets[0]["role"] != "canonical":
        raise ContractValidationError(
            "canonical_target",
            f"{path}.candidate_targets",
            "first target must be canonical",
        )
    require_unique(
        [target["candidate_target_id"] for target in targets],
        path=f"{path}.candidate_targets[*].candidate_target_id",
    )
    source_occurrences = _string_list(
        row["source_occurrences"], path=f"{path}.source_occurrences", minimum=1
    )
    context_ids = {context["context_id"] for context in contexts}
    if not set(source_occurrences).issubset(context_ids):
        raise ContractValidationError(
            "source_occurrence_binding",
            f"{path}.source_occurrences",
            "source occurrences must identify contexts in this term",
        )
    sense_contract = _validate_sense_contract(
        row["sense_contract"], path=f"{path}.sense_contract"
    )
    return {
        "term_id": require_string(row["term_id"], path=f"{path}.term_id"),
        "source_term": require_string(
            row["source_term"], path=f"{path}.source_term", maximum=500
        ),
        "sense_id": require_string(
            row["sense_id"], path=f"{path}.sense_id", maximum=500
        ),
        "scope_id": require_string(
            row["scope_id"], path=f"{path}.scope_id", maximum=500
        ),
        "sense_contract": sense_contract,
        "part_of_speech": require_string(
            row["part_of_speech"], path=f"{path}.part_of_speech", maximum=100
        ),
        "source_occurrences": source_occurrences,
        "contexts": contexts,
        "candidate_targets": targets,
    }


def _validate_context(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "context_id",
            "chapter_id",
            "block_id",
            "block_type",
            "source_text",
            "source_text_sha256",
            "source_provenance",
            "reviewed_selection",
        },
        path=path,
    )
    source_text = require_string(
        row["source_text"], path=f"{path}.source_text", allow_empty=True
    )
    source_sha256 = require_sha256(
        row["source_text_sha256"], path=f"{path}.source_text_sha256"
    )
    if source_sha256 != sha256_text(source_text):
        raise ContractValidationError(
            "source_hash", f"{path}.source_text_sha256", "source text hash mismatch"
        )
    provenance = validate_source_provenance(
        row["source_provenance"],
        path=f"{path}.source_provenance",
        source_text=source_text,
    )
    chapter_id = require_string(row["chapter_id"], path=f"{path}.chapter_id")
    block_id = require_string(row["block_id"], path=f"{path}.block_id")
    if provenance["chapter_id"] != chapter_id or provenance["block_id"] != block_id:
        raise ContractValidationError(
            "source_provenance",
            f"{path}.source_provenance",
            "physical chapter/block locator differs from the context",
        )
    if provenance["source_hash"] != source_sha256:
        raise ContractValidationError(
            "source_provenance",
            f"{path}.source_provenance.source_hash",
            "source hash differs from the context",
        )
    return {
        "context_id": require_string(
            row["context_id"], path=f"{path}.context_id", maximum=500
        ),
        "chapter_id": chapter_id,
        "block_id": block_id,
        "block_type": require_string(
            row["block_type"], path=f"{path}.block_type", maximum=100
        ),
        "source_text": source_text,
        "source_text_sha256": source_sha256,
        "source_provenance": provenance,
        "reviewed_selection": _validate_reviewed_selection(
            row["reviewed_selection"], path=f"{path}.reviewed_selection"
        ),
    }


def _validate_target(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "candidate_target_id",
            "role",
            "target_vi",
            "applicability",
            "candidate_generation",
        },
        path=path,
    )
    return {
        "candidate_target_id": require_string(
            row["candidate_target_id"], path=f"{path}.candidate_target_id"
        ),
        "role": require_enum(row["role"], TARGET_ROLES, path=f"{path}.role"),
        "target_vi": require_string(
            row["target_vi"], path=f"{path}.target_vi", maximum=500
        ),
        "applicability": require_nullable_string(
            row["applicability"], path=f"{path}.applicability", maximum=1_000
        ),
        "candidate_generation": _validate_candidate_generation(
            row["candidate_generation"], path=f"{path}.candidate_generation"
        ),
    }


def _validate_sense_contract(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "definition_en",
            "definition_source",
            "definition_provenance",
            "definition_review_status",
            "sense_inventory_version",
        },
        path=path,
    )
    return {
        "definition_en": require_string(
            row["definition_en"], path=f"{path}.definition_en", maximum=4_000
        ),
        "definition_source": require_string(
            row["definition_source"], path=f"{path}.definition_source", maximum=500
        ),
        "definition_provenance": _string_list(
            row["definition_provenance"],
            path=f"{path}.definition_provenance",
            minimum=1,
        ),
        "definition_review_status": require_enum(
            row["definition_review_status"],
            SENSE_DEFINITION_STATUSES,
            path=f"{path}.definition_review_status",
        ),
        "sense_inventory_version": require_string(
            row["sense_inventory_version"],
            path=f"{path}.sense_inventory_version",
            maximum=500,
        ),
    }


def _validate_candidate_generation(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "generator_model",
            "prompt_version",
            "run_id",
            "recording_status",
            "candidate_version",
            "candidate_slot_id",
            "candidate_slot_status",
            "formation_method",
        },
        path=path,
    )
    result = {
        key: require_nullable_string(row[key], path=f"{path}.{key}", maximum=500)
        for key in ("generator_model", "prompt_version", "run_id")
    }
    status = require_enum(
        row["recording_status"],
        {"RECORDED", "UNAVAILABLE_IN_SEALED_ARTIFACT"},
        path=f"{path}.recording_status",
    )
    if status == "RECORDED" and not any(result.values()):
        raise ContractValidationError(
            "candidate_generation", path, "RECORDED requires an identifier"
        )
    if status == "UNAVAILABLE_IN_SEALED_ARTIFACT" and any(result.values()):
        raise ContractValidationError(
            "candidate_generation", path, "unavailable metadata must remain null"
        )
    candidate_version = require_nullable_string(
        row["candidate_version"],
        path=f"{path}.candidate_version",
        maximum=64,
    )
    if candidate_version is not None:
        candidate_version = require_sha256(
            candidate_version, path=f"{path}.candidate_version"
        )
    slot_id = require_nullable_string(
        row["candidate_slot_id"],
        path=f"{path}.candidate_slot_id",
        maximum=500,
    )
    source_status = require_enum(
        row["candidate_slot_status"],
        SOURCE_CANDIDATE_STATUSES,
        path=f"{path}.candidate_slot_status",
    )
    formation_method = require_nullable_string(
        row["formation_method"],
        path=f"{path}.formation_method",
        maximum=500,
    )
    if source_status == "UNAVAILABLE_IN_LEGACY_ARTIFACT":
        if candidate_version is not None or slot_id is not None or formation_method is not None:
            raise ContractValidationError(
                "candidate_version",
                path,
                "legacy-unavailable candidate metadata must remain null",
            )
    elif candidate_version is None or slot_id is None or formation_method is None:
        raise ContractValidationError(
            "candidate_version",
            path,
            "reviewed-support candidates require version, slot, and formation metadata",
        )
    return {
        **result,
        "recording_status": status,
        "candidate_version": candidate_version,
        "candidate_slot_id": slot_id,
        "candidate_slot_status": source_status,
        "formation_method": formation_method,
    }


def _validate_selection_contract(
    value: Any,
    *,
    origin: Mapping[str, str],
) -> dict[str, Any]:
    path = "$.selection_contract"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "selector_mode",
            "authority_status",
            "dataset_manifest_sha256",
            "parent_dataset_manifest_sha256",
            "review_artifact_ref",
            "review_artifact_sha256",
            "effective_sense_contract_ref",
            "effective_sense_contract_sha256",
        },
        path=path,
    )
    mode = require_enum(row["selector_mode"], SELECTOR_MODES, path=f"{path}.selector_mode")
    authority = require_enum(
        row["authority_status"],
        SELECTION_AUTHORITY_STATUSES,
        path=f"{path}.authority_status",
    )
    dataset_hash = require_sha256(
        row["dataset_manifest_sha256"],
        path=f"{path}.dataset_manifest_sha256",
    )
    parent_hash = _nullable_sha256(
        row["parent_dataset_manifest_sha256"],
        path=f"{path}.parent_dataset_manifest_sha256",
    )
    review_ref = require_nullable_string(
        row["review_artifact_ref"], path=f"{path}.review_artifact_ref", maximum=4_000
    )
    review_hash = _nullable_sha256(
        row["review_artifact_sha256"], path=f"{path}.review_artifact_sha256"
    )
    sense_ref = require_nullable_string(
        row["effective_sense_contract_ref"],
        path=f"{path}.effective_sense_contract_ref",
        maximum=4_000,
    )
    sense_hash = _nullable_sha256(
        row["effective_sense_contract_sha256"],
        path=f"{path}.effective_sense_contract_sha256",
    )
    if dataset_hash != origin["source_sha256"]:
        raise ContractValidationError(
            "selection_binding",
            f"{path}.dataset_manifest_sha256",
            "selector dataset must equal the input origin",
        )
    if mode == "MODEL_CLASSIFICATION_DEVELOPMENT":
        if authority != "DEVELOPMENT_PENDING_HUMAN_REVIEW":
            raise ContractValidationError(
                "selection_authority", path, "development selector cannot claim frozen authority"
            )
        if any(value is not None for value in (review_ref, review_hash, sense_ref, sense_hash)):
            raise ContractValidationError(
                "selection_binding", path, "development selector cannot bind human-review artifacts"
            )
    else:
        if authority != "FROZEN_HUMAN_REVIEWED":
            raise ContractValidationError(
                "selection_authority", path, "frozen selector requires frozen human authority"
            )
        if any(value is None for value in (review_ref, review_hash, sense_ref, sense_hash)):
            raise ContractValidationError(
                "selection_binding", path, "frozen selector requires both immutable review artifacts"
            )
        if origin["kind"] != "FROZEN_HUMAN_REVIEWED_PILOT_V1":
            raise ContractValidationError(
                "selection_binding", path, "frozen selector requires frozen reviewed pilot origin"
            )
    return {
        "selector_mode": mode,
        "authority_status": authority,
        "dataset_manifest_sha256": dataset_hash,
        "parent_dataset_manifest_sha256": parent_hash,
        "review_artifact_ref": review_ref,
        "review_artifact_sha256": review_hash,
        "effective_sense_contract_ref": sense_ref,
        "effective_sense_contract_sha256": sense_hash,
    }


def _validate_reviewed_selection(value: Any, *, path: str) -> dict[str, str] | None:
    if value is None:
        return None
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "sense_relation",
            "context_type",
            "judgeability",
            "reason",
            "review_row_sha256",
        },
        path=path,
    )
    from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.responses import (
        validate_selector_annotation,
    )

    validated = validate_selector_annotation(
        {
            "context_id": "frozen-review-placeholder",
            "sense_relation": row["sense_relation"],
            "context_type": row["context_type"],
            "judgeability": row["judgeability"],
            "reason": row["reason"],
        },
        path=path,
    )
    return {
        "sense_relation": validated["sense_relation"],
        "context_type": validated["context_type"],
        "judgeability": validated["judgeability"],
        "reason": validated["reason"],
        "review_row_sha256": require_sha256(
            row["review_row_sha256"], path=f"{path}.review_row_sha256"
        ),
    }


def _development_selection_contract(
    *,
    dataset_manifest_sha256: str,
    parent_dataset_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "selector_mode": "MODEL_CLASSIFICATION_DEVELOPMENT",
        "authority_status": "DEVELOPMENT_PENDING_HUMAN_REVIEW",
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "parent_dataset_manifest_sha256": parent_dataset_manifest_sha256,
        "review_artifact_ref": None,
        "review_artifact_sha256": None,
        "effective_sense_contract_ref": None,
        "effective_sense_contract_sha256": None,
    }


def _with_compatibility_defaults(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    origin = result.get("input_origin")
    if "selection_contract" not in result and isinstance(origin, Mapping):
        source_hash = origin.get("source_sha256")
        if isinstance(source_hash, str):
            result["selection_contract"] = _development_selection_contract(
                dataset_manifest_sha256=source_hash
            )
    terms = []
    for raw_term in result.get("terms", []):
        term = dict(raw_term)
        term["contexts"] = [
            {**dict(context), "reviewed_selection": context.get("reviewed_selection")}
            for context in term.get("contexts", [])
        ]
        targets = []
        for raw_target in term.get("candidate_targets", []):
            target = dict(raw_target)
            generation = dict(target.get("candidate_generation", {}))
            generation.setdefault("candidate_version", None)
            generation.setdefault("candidate_slot_id", None)
            generation.setdefault(
                "candidate_slot_status", "UNAVAILABLE_IN_LEGACY_ARTIFACT"
            )
            generation.setdefault("formation_method", None)
            target["candidate_generation"] = generation
            targets.append(target)
        term["candidate_targets"] = targets
        terms.append(term)
    result["terms"] = terms
    result["schema_version"] = INPUT_SCHEMA_VERSION
    return result


def _nullable_sha256(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    return require_sha256(value, path=path)


def _string_list(value: Any, *, path: str, minimum: int) -> list[str]:
    result = [
        require_string(child, path=f"{path}[{index}]")
        for index, child in enumerate(require_list(value, path=path))
    ]
    if len(result) < minimum:
        raise ContractValidationError(
            "list_length", path, f"expected at least {minimum} entries"
        )
    require_unique(result, path=path)
    return result
