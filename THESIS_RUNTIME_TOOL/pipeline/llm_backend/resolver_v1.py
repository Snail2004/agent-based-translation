"""Deterministic, credential-free resolution of pipeline profiles into run seals."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from .contracts_v1 import (
    ContractValidationError,
    REUSABLE_ARTIFACT_RECEIPT_SCHEMA_VERSION,
    _closed_object,
    _identifier,
    _integer,
    _namespace_identifier,
    _reject_evaluation_authority_identifier,
    _reject_forbidden_recursive,
    _sha256,
    canonical_sha256,
    validate_api_source,
    validate_cache_observation,
    validate_capability_evidence,
    validate_llm_attempt_usage,
    validate_llm_error,
    validate_pipeline_profile,
    validate_reusable_artifact_receipt,
)


RESOLVED_LLM_RUN_SEAL_SCHEMA_VERSION = "resolved_llm_run_seal_v1"


def resolve_llm_run_seal(
    *,
    profile: Mapping[str, Any],
    api_sources: Sequence[Mapping[str, Any]],
    capability_evidence: Sequence[Mapping[str, Any]],
    role_id: str,
    run_id: str,
    attempt_run_id: str,
    stage_id: str,
    input_bindings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve one role without loading credentials or calling a provider."""

    normalized_profile = validate_pipeline_profile(profile)
    selected_role_id = _identifier(role_id, "role_id")
    selected = next(
        (
            row
            for row in normalized_profile["role_bindings"]
            if row["role_id"] == selected_role_id
        ),
        None,
    )
    if selected is None:
        raise ContractValidationError(f"profile lacks role {selected_role_id}")

    profile_wrapper = {
        "record": normalized_profile,
        "sha256": canonical_sha256(normalized_profile),
    }
    role_wrapper = {
        "record": deepcopy(selected),
        "sha256": canonical_sha256(selected),
    }
    validated_inputs = _validate_input_bindings(
        input_bindings, workstream=normalized_profile["workstream"]
    )
    input_bindings_sha256 = canonical_sha256(validated_inputs)

    source_catalog = _source_catalog(api_sources)
    capability_catalog = _capability_catalog(capability_evidence)
    primary = _resolve_target(
        selected["primary"],
        role=selected,
        source_catalog=source_catalog,
        capability_catalog=capability_catalog,
        label="primary",
    )
    fallback_steps = [
        _resolve_target(
            target,
            role=selected,
            source_catalog=source_catalog,
            capability_catalog=capability_catalog,
            label=f"fallback step {index}",
        )
        for index, target in enumerate(selected["fallback_plan"]["steps"], start=1)
    ]

    resolved_run_id = _identifier(run_id, "run_id")
    resolved_attempt_id = _identifier(attempt_run_id, "attempt_run_id")
    resolved_stage_id = _identifier(stage_id, "stage_id")
    namespace_material_sha256 = _namespace_material_sha256(
        profile_wrapper["sha256"],
        role_wrapper["sha256"],
        input_bindings_sha256,
        resolved_stage_id,
    )
    resolved_namespaces = _resolved_namespaces(
        selected["namespaces"],
        namespace_material_sha256=namespace_material_sha256,
        run_id=resolved_run_id,
        attempt_run_id=resolved_attempt_id,
    )
    body = {
        "schema_version": RESOLVED_LLM_RUN_SEAL_SCHEMA_VERSION,
        "run_id": resolved_run_id,
        "attempt_run_id": resolved_attempt_id,
        "stage_id": resolved_stage_id,
        "workstream": normalized_profile["workstream"],
        "role_id": selected_role_id,
        "profile": profile_wrapper,
        "role_binding": role_wrapper,
        "input_bindings_sha256": input_bindings_sha256,
        "namespace_material_sha256": namespace_material_sha256,
        "primary": primary,
        "fallback_plan": {
            "enabled": selected["fallback_plan"]["enabled"],
            "steps": fallback_steps,
        },
        "input_bindings": validated_inputs,
        **resolved_namespaces,
    }
    sealed = {**body, "seal_sha256": canonical_sha256(body)}
    return validate_resolved_llm_run_seal(sealed)


def validate_resolved_llm_run_seal(value: Mapping[str, Any]) -> dict[str, Any]:
    seal = _closed_object(
        value,
        {
            "schema_version",
            "run_id",
            "attempt_run_id",
            "stage_id",
            "workstream",
            "role_id",
            "profile",
            "role_binding",
            "input_bindings_sha256",
            "namespace_material_sha256",
            "primary",
            "fallback_plan",
            "input_bindings",
            "output_root_id",
            "checkpoint_namespace",
            "cache_namespace",
            "seal_sha256",
        },
        "resolved run seal",
    )
    if seal["schema_version"] != RESOLVED_LLM_RUN_SEAL_SCHEMA_VERSION:
        raise ContractValidationError("foreign resolved run seal schema")
    for field in ("run_id", "attempt_run_id", "stage_id", "role_id"):
        seal[field] = _identifier(seal[field], field)
    seal["seal_sha256"] = _sha256(seal["seal_sha256"], "seal_sha256")
    seal["namespace_material_sha256"] = _sha256(
        seal["namespace_material_sha256"], "namespace_material_sha256"
    )
    seal["input_bindings_sha256"] = _sha256(
        seal["input_bindings_sha256"], "input_bindings_sha256"
    )

    profile_wrapper = _closed_object(
        seal["profile"], {"record", "sha256"}, "sealed profile"
    )
    profile_wrapper["record"] = validate_pipeline_profile(profile_wrapper["record"])
    profile_wrapper["sha256"] = _sha256(
        profile_wrapper["sha256"], "profile.sha256"
    )
    if canonical_sha256(profile_wrapper["record"]) != profile_wrapper["sha256"]:
        raise ContractValidationError("sealed profile hash mismatch")
    seal["profile"] = profile_wrapper
    if seal["workstream"] != profile_wrapper["record"]["workstream"]:
        raise ContractValidationError("seal workstream differs from profile")

    role_wrapper = _closed_object(
        seal["role_binding"], {"record", "sha256"}, "sealed role binding"
    )
    role_wrapper["sha256"] = _sha256(
        role_wrapper["sha256"], "role_binding.sha256"
    )
    selected = next(
        (
            role
            for role in profile_wrapper["record"]["role_bindings"]
            if role["role_id"] == seal["role_id"]
        ),
        None,
    )
    if selected is None:
        raise ContractValidationError("sealed role is absent from profile")
    if role_wrapper["record"] != selected:
        raise ContractValidationError("sealed role binding differs from profile")
    if canonical_sha256(selected) != role_wrapper["sha256"]:
        raise ContractValidationError("sealed role binding hash mismatch")
    role_wrapper["record"] = deepcopy(selected)
    seal["role_binding"] = role_wrapper
    seal["primary"] = _validate_resolved_target(
        seal["primary"], target=selected["primary"], role=selected, label="primary"
    )
    fallback = _closed_object(
        seal["fallback_plan"], {"enabled", "steps"}, "sealed fallback plan"
    )
    if fallback["enabled"] != selected["fallback_plan"]["enabled"]:
        raise ContractValidationError("sealed fallback enabled state differs from profile")
    if not isinstance(fallback["steps"], list) or len(fallback["steps"]) != len(
        selected["fallback_plan"]["steps"]
    ):
        raise ContractValidationError("sealed fallback steps differ from profile")
    fallback["steps"] = [
        _validate_resolved_target(row, target=target, role=selected, label=f"fallback {index}")
        for index, (row, target) in enumerate(
            zip(fallback["steps"], selected["fallback_plan"]["steps"], strict=True),
            start=1,
        )
    ]
    seal["fallback_plan"] = fallback
    seal["input_bindings"] = _validate_input_bindings(
        seal["input_bindings"], workstream=seal["workstream"]
    )
    expected_input_bindings_sha256 = canonical_sha256(seal["input_bindings"])
    if seal["input_bindings_sha256"] != expected_input_bindings_sha256:
        raise ContractValidationError("sealed input binding hash mismatch")
    expected_namespace_material = _namespace_material_sha256(
        profile_wrapper["sha256"],
        role_wrapper["sha256"],
        seal["input_bindings_sha256"],
        seal["stage_id"],
    )
    if seal["namespace_material_sha256"] != expected_namespace_material:
        raise ContractValidationError("sealed namespace material mismatch")

    for field in ("output_root_id", "checkpoint_namespace", "cache_namespace"):
        seal[field] = _namespace_identifier(seal[field], field)
    expected_namespaces = _resolved_namespaces(
        selected["namespaces"],
        namespace_material_sha256=seal["namespace_material_sha256"],
        run_id=seal["run_id"],
        attempt_run_id=seal["attempt_run_id"],
    )
    if seal["output_root_id"] != expected_namespaces["output_root_id"]:
        raise ContractValidationError("sealed output root identity mismatch")
    if seal["checkpoint_namespace"] != expected_namespaces["checkpoint_namespace"]:
        raise ContractValidationError("sealed checkpoint namespace mismatch")
    if seal["cache_namespace"] != expected_namespaces["cache_namespace"]:
        raise ContractValidationError("sealed cache namespace mismatch")

    observed_hash = seal.pop("seal_sha256")
    if canonical_sha256(seal) != observed_hash:
        raise ContractValidationError("resolved run seal hash mismatch")
    seal["seal_sha256"] = observed_hash
    _reject_forbidden_recursive(seal, "resolved run seal")
    return seal


def derive_llm_attempt_identity(
    *,
    seal: Mapping[str, Any],
    logical_request_id: str,
    semantic_attempt_index: int,
    transport_retry_ordinal: int,
) -> dict[str, Any]:
    """Derive immutable request and physical-attempt identities from one seal."""

    normalized = validate_resolved_llm_run_seal(seal)
    request_id = _identifier(logical_request_id, "logical_request_id")
    semantic_index = _integer(
        semantic_attempt_index, "semantic_attempt_index", minimum=1
    )
    transport_ordinal = _integer(
        transport_retry_ordinal, "transport_retry_ordinal", minimum=0
    )
    logical_sha256 = _logical_request_sha256(normalized, request_id)
    return {
        "logical_request_id": request_id,
        "logical_request_sha256": logical_sha256,
        "semantic_attempt_index": semantic_index,
        "transport_retry_ordinal": transport_ordinal,
        "attempt_usage_id": _attempt_usage_id(
            seal_sha256=normalized["seal_sha256"],
            logical_request_sha256=logical_sha256,
            semantic_attempt_index=semantic_index,
            transport_retry_ordinal=transport_ordinal,
        ),
    }


def derive_cache_key_sha256(
    *, seal: Mapping[str, Any], logical_request_id: str, cache_kind: str
) -> str:
    """Derive an input- and profile-bound cache key for one logical request."""

    normalized = validate_resolved_llm_run_seal(seal)
    request_id = _identifier(logical_request_id, "logical_request_id")
    if cache_kind not in {
        "provider_prompt_cache",
        "application_response_cache",
        "retrieval_context_cache",
        "checkpoint_stage_reuse",
    }:
        raise ContractValidationError("cache key requires a concrete cache kind")
    return _cache_key_sha256(
        cache_namespace=normalized["cache_namespace"],
        cache_kind=cache_kind,
        logical_request_sha256=_logical_request_sha256(normalized, request_id),
    )


def create_reusable_artifact_receipt(
    *,
    producer_seal: Mapping[str, Any],
    logical_request_id: str,
    artifact_kind: str,
    artifact_sha256: str,
    created_at_utc: str,
) -> dict[str, Any]:
    """Create a content-addressed receipt for one durable reusable artifact."""

    normalized = validate_resolved_llm_run_seal(producer_seal)
    request_id = _identifier(logical_request_id, "logical_request_id")
    body = {
        "schema_version": REUSABLE_ARTIFACT_RECEIPT_SCHEMA_VERSION,
        "producer_seal_sha256": normalized["seal_sha256"],
        "producer_logical_request_id": request_id,
        "producer_logical_request_sha256": _logical_request_sha256(
            normalized, request_id
        ),
        "artifact_kind": artifact_kind,
        "artifact_sha256": _sha256(artifact_sha256, "artifact_sha256"),
        "created_at_utc": created_at_utc,
    }
    return validate_reusable_artifact_receipt(
        {**body, "receipt_sha256": canonical_sha256(body)}
    )


def validate_llm_run_records(
    *,
    seal: Mapping[str, Any],
    usage_rows: Sequence[Mapping[str, Any]] = (),
    error_rows: Sequence[Mapping[str, Any]] = (),
    cache_observations: Sequence[Mapping[str, Any]] = (),
    producer_seals: Sequence[Mapping[str, Any]] = (),
    reusable_artifact_receipts: Sequence[Mapping[str, Any]] = (),
    certify_limits: bool = True,
) -> dict[str, Any]:
    """Validate usage, errors and cache observations against one run seal."""

    if not isinstance(certify_limits, bool):
        raise ContractValidationError("certify_limits must be a boolean")
    normalized_seal = validate_resolved_llm_run_seal(seal)
    usage = _validated_sequence(
        usage_rows, validate_llm_attempt_usage, "usage_rows"
    )
    errors = _validated_sequence(error_rows, validate_llm_error, "error_rows")
    cache = _validated_sequence(
        cache_observations, validate_cache_observation, "cache_observations"
    )
    trusted_producer_seals = _validated_sequence(
        producer_seals, validate_resolved_llm_run_seal, "producer_seals"
    )
    receipts = _validated_sequence(
        reusable_artifact_receipts,
        validate_reusable_artifact_receipt,
        "reusable_artifact_receipts",
    )
    seal_sha256 = normalized_seal["seal_sha256"]
    role = normalized_seal["role_binding"]["record"]
    producer_seal_by_hash = {seal_sha256: normalized_seal}
    for producer in trusted_producer_seals:
        producer_hash = producer["seal_sha256"]
        if producer_hash in producer_seal_by_hash and producer_seal_by_hash[
            producer_hash
        ] != producer:
            raise ContractValidationError("producer_seals repeats a hash with different bytes")
        producer_seal_by_hash[producer_hash] = producer
    receipt_by_hash: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        receipt_hash = receipt["receipt_sha256"]
        if receipt_hash in receipt_by_hash:
            raise ContractValidationError("reusable artifact receipts repeat receipt_sha256")
        receipt_by_hash[receipt_hash] = receipt

    resolved_targets = [normalized_seal["primary"], *normalized_seal["fallback_plan"]["steps"]]
    allowed_targets = {
        (
            target["source"]["source_id"],
            target["source"]["source_revision"],
            target["source"]["physical_quota_bucket_id"],
            target["target"]["requested_model_id"],
            target["capability"]["observed_model_id"],
        )
        for target in resolved_targets
    }

    usage_ids: set[str] = set()
    request_ids: set[str] = set()
    attempt_indexes: set[int] = set()
    usage_by_id: dict[str, dict[str, Any]] = {}
    attempt_lineages: set[tuple[str, int, int]] = set()
    for row in usage:
        if row["seal_sha256"] != seal_sha256:
            raise ContractValidationError("usage row is bound to a foreign seal")
        matching = [
            target
            for target in allowed_targets
            if row["source_id"] == target[0]
            and row["source_revision"] == target[1]
            and row["physical_quota_bucket_id"] == target[2]
            and row["requested_model_id"] == target[3]
        ]
        if len(matching) != 1:
            raise ContractValidationError(
                "usage row source, bucket or requested model differs from the seal"
            )
        if row["observed_model_id"] is not None and row[
            "observed_model_id"
        ] != matching[0][4]:
            raise ContractValidationError("usage observed model differs from capability evidence")
        expected_logical_sha256 = _logical_request_sha256(
            normalized_seal, row["logical_request_id"]
        )
        if row["logical_request_sha256"] != expected_logical_sha256:
            raise ContractValidationError("usage logical request lineage differs from the seal")
        expected_attempt_usage_id = _attempt_usage_id(
            seal_sha256=seal_sha256,
            logical_request_sha256=expected_logical_sha256,
            semantic_attempt_index=row["semantic_attempt_index"],
            transport_retry_ordinal=row["transport_retry_ordinal"],
        )
        if row["attempt_usage_id"] != expected_attempt_usage_id:
            raise ContractValidationError("usage attempt identity differs from sealed lineage")
        lineage = (
            expected_logical_sha256,
            row["semantic_attempt_index"],
            row["transport_retry_ordinal"],
        )
        if lineage in attempt_lineages:
            raise ContractValidationError("usage_rows repeats a logical physical attempt")
        attempt_lineages.add(lineage)
        if row["attempt_usage_id"] in usage_ids:
            raise ContractValidationError("usage_rows repeats attempt_usage_id")
        usage_ids.add(row["attempt_usage_id"])
        usage_by_id[row["attempt_usage_id"]] = row
        if row["physical_attempt_index"] in attempt_indexes:
            raise ContractValidationError("usage_rows repeats physical_attempt_index")
        attempt_indexes.add(row["physical_attempt_index"])
        if row["request_id"] is not None:
            if row["request_id"] in request_ids:
                raise ContractValidationError("usage_rows repeats provider request_id")
            request_ids.add(row["request_id"])

    expected_indexes = set(range(1, len(usage) + 1))
    if attempt_indexes != expected_indexes:
        raise ContractValidationError(
            "physical attempt indexes must be contiguous and start at one"
        )
    limits = role["limits"]
    if len(usage) > limits["max_calls"]:
        raise ContractValidationError("physical attempts exceed the sealed call cap")
    if certify_limits:
        _validate_usage_totals(usage, role)

    error_ids: set[str] = set()
    errors_by_id: dict[str, dict[str, Any]] = {}
    for row in errors:
        if row["seal_sha256"] != seal_sha256:
            raise ContractValidationError("error row is bound to a foreign seal")
        if row["error_id"] in error_ids:
            raise ContractValidationError("error_rows repeats error_id")
        error_ids.add(row["error_id"])
        errors_by_id[row["error_id"]] = row
        attempt_usage_id = row["attempt_usage_id"]
        if attempt_usage_id is None or attempt_usage_id not in usage_by_id:
            raise ContractValidationError("error row lacks its sealed attempt usage")
        if usage_by_id[attempt_usage_id]["error_id"] != row["error_id"]:
            raise ContractValidationError("error and usage back-references differ")
        _validate_error_retry_against_seal(row, role)

    for row in usage:
        if row["outcome"] != "succeeded" and row["error_id"] not in errors_by_id:
            raise ContractValidationError("failed usage lacks its error record")
    _validate_retry_sequences(usage, errors_by_id, role)

    observation_ids: set[str] = set()
    for row in cache:
        if row["seal_sha256"] != seal_sha256:
            raise ContractValidationError("cache observation is bound to a foreign seal")
        if row["cache_namespace"] != normalized_seal["cache_namespace"]:
            raise ContractValidationError("cache observation uses a foreign namespace")
        expected_logical_sha256 = _logical_request_sha256(
            normalized_seal, row["logical_request_id"]
        )
        if row["logical_request_sha256"] != expected_logical_sha256:
            raise ContractValidationError("cache logical request lineage differs from the seal")
        if row["cache_kind"] != "none":
            expected_cache_key = _cache_key_sha256(
                cache_namespace=normalized_seal["cache_namespace"],
                cache_kind=row["cache_kind"],
                logical_request_sha256=expected_logical_sha256,
            )
            if row["cache_key_sha256"] != expected_cache_key:
                raise ContractValidationError("cache key differs from sealed request input")
        attempt_usage_id = row["attempt_usage_id"]
        if attempt_usage_id is not None:
            if attempt_usage_id not in usage_by_id:
                raise ContractValidationError("cache observation references foreign attempt usage")
            if usage_by_id[attempt_usage_id]["logical_request_sha256"] != expected_logical_sha256:
                raise ContractValidationError("cache and attempt logical requests differ")
            if (
                row["cache_kind"] == "provider_prompt_cache"
                and row["lookup_status"] == "hit"
                and row["provider_cached_input_tokens"]
                != usage_by_id[attempt_usage_id]["cached_input_tokens"]
            ):
                raise ContractValidationError(
                    "provider prompt-cache usage and observation token facts differ"
                )
        if row["cache_kind"] in {
            "application_response_cache",
            "checkpoint_stage_reuse",
        } and row["lookup_status"] == "hit":
            _validate_reusable_cache_lineage(
                observation=row,
                consumer_seal=normalized_seal,
                producer_seal_by_hash=producer_seal_by_hash,
                receipt_by_hash=receipt_by_hash,
            )
        if row["observation_id"] in observation_ids:
            raise ContractValidationError("cache observations repeat observation_id")
        observation_ids.add(row["observation_id"])

    return {
        "seal": normalized_seal,
        "usage_rows": sorted(usage, key=lambda row: row["physical_attempt_index"]),
        "error_rows": sorted(errors, key=lambda row: row["error_id"]),
        "cache_observations": sorted(cache, key=lambda row: row["observation_id"]),
        "producer_seals": sorted(
            trusted_producer_seals, key=lambda row: row["seal_sha256"]
        ),
        "reusable_artifact_receipts": sorted(
            receipts, key=lambda row: row["receipt_sha256"]
        ),
        "limits_certified": certify_limits,
    }


def _validate_reusable_cache_lineage(
    *,
    observation: Mapping[str, Any],
    consumer_seal: Mapping[str, Any],
    producer_seal_by_hash: Mapping[str, Mapping[str, Any]],
    receipt_by_hash: Mapping[str, Mapping[str, Any]],
) -> None:
    producer_hash = observation["producer_seal_sha256"]
    producer = producer_seal_by_hash.get(producer_hash)
    if producer is None:
        raise ContractValidationError("reused artifact producer seal is not trusted")
    receipt_hash = observation["producer_artifact_receipt_sha256"]
    receipt = receipt_by_hash.get(receipt_hash)
    if receipt is None:
        raise ContractValidationError("reused artifact receipt is not trusted")
    if receipt["producer_seal_sha256"] != producer_hash:
        raise ContractValidationError("artifact receipt belongs to a different producer seal")
    if receipt["artifact_sha256"] != observation["reused_artifact_sha256"]:
        raise ContractValidationError("artifact receipt hash differs from reused artifact")
    expected_kind = {
        "application_response_cache": "application_response",
        "checkpoint_stage_reuse": "checkpoint_stage",
    }[observation["cache_kind"]]
    if receipt["artifact_kind"] != expected_kind:
        raise ContractValidationError("artifact receipt kind differs from cache kind")
    if receipt["producer_logical_request_id"] != observation["logical_request_id"]:
        raise ContractValidationError("artifact receipt belongs to a different logical request")
    expected_producer_request = _logical_request_sha256(
        producer, receipt["producer_logical_request_id"]
    )
    if receipt["producer_logical_request_sha256"] != expected_producer_request:
        raise ContractValidationError("artifact receipt request lineage differs from producer seal")
    if observation["producer_input_bindings_sha256"] != producer[
        "input_bindings_sha256"
    ]:
        raise ContractValidationError("cache observation misstates producer inputs")

    compatibility_fields = (
        "workstream",
        "role_id",
        "stage_id",
        "input_bindings_sha256",
        "namespace_material_sha256",
        "cache_namespace",
    )
    for field in compatibility_fields:
        if producer[field] != consumer_seal[field]:
            raise ContractValidationError(
                f"reused artifact producer {field} differs from consumer seal"
            )
    if producer["profile"]["sha256"] != consumer_seal["profile"]["sha256"]:
        raise ContractValidationError("reused artifact producer profile differs from consumer")
    if producer["role_binding"]["sha256"] != consumer_seal["role_binding"]["sha256"]:
        raise ContractValidationError("reused artifact producer role differs from consumer")
    if receipt["producer_logical_request_sha256"] != observation[
        "logical_request_sha256"
    ]:
        raise ContractValidationError("producer and consumer request lineages differ")


def _validated_sequence(value: Any, validator, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractValidationError(f"{label} must be a sequence")
    return [validator(row) for row in value]


def _logical_request_sha256(
    seal: Mapping[str, Any], logical_request_id: str
) -> str:
    return canonical_sha256(
        {
            "namespace_material_sha256": seal["namespace_material_sha256"],
            "role_id": seal["role_id"],
            "stage_id": seal["stage_id"],
            "logical_request_id": _identifier(
                logical_request_id, "logical_request_id"
            ),
        }
    )


def _attempt_usage_id(
    *,
    seal_sha256: str,
    logical_request_sha256: str,
    semantic_attempt_index: int,
    transport_retry_ordinal: int,
) -> str:
    return canonical_sha256(
        {
            "seal_sha256": _sha256(seal_sha256, "seal_sha256"),
            "logical_request_sha256": _sha256(
                logical_request_sha256, "logical_request_sha256"
            ),
            "semantic_attempt_index": _integer(
                semantic_attempt_index, "semantic_attempt_index", minimum=1
            ),
            "transport_retry_ordinal": _integer(
                transport_retry_ordinal, "transport_retry_ordinal", minimum=0
            ),
        }
    )


def _cache_key_sha256(
    *, cache_namespace: str, cache_kind: str, logical_request_sha256: str
) -> str:
    return canonical_sha256(
        {
            "cache_namespace": _namespace_identifier(
                cache_namespace, "cache_namespace"
            ),
            "cache_kind": cache_kind,
            "logical_request_sha256": _sha256(
                logical_request_sha256, "logical_request_sha256"
            ),
        }
    )


def _validate_retry_sequences(
    usage: Sequence[Mapping[str, Any]],
    errors_by_id: Mapping[str, Mapping[str, Any]],
    role: Mapping[str, Any],
) -> None:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in usage:
        grouped.setdefault(row["logical_request_sha256"], []).append(row)

    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: row["physical_attempt_index"])
        expected_order = sorted(
            rows,
            key=lambda row: (
                row["semantic_attempt_index"],
                row["transport_retry_ordinal"],
            ),
        )
        if [row["attempt_usage_id"] for row in ordered] != [
            row["attempt_usage_id"] for row in expected_order
        ]:
            raise ContractValidationError(
                "physical attempts do not follow semantic and transport retry order"
            )

        semantic_groups: dict[int, list[Mapping[str, Any]]] = {}
        for row in ordered:
            semantic_groups.setdefault(row["semantic_attempt_index"], []).append(row)
        semantic_indexes = sorted(semantic_groups)
        if semantic_indexes != list(range(1, len(semantic_indexes) + 1)):
            raise ContractValidationError(
                "semantic attempt indexes must be contiguous and start at one"
            )
        if len(semantic_indexes) > role["semantic_retry"]["max_retries"] + 1:
            raise ContractValidationError("semantic attempts exceed the sealed retry cap")

        for semantic_index in semantic_indexes:
            attempts = sorted(
                semantic_groups[semantic_index],
                key=lambda row: row["transport_retry_ordinal"],
            )
            ordinals = [row["transport_retry_ordinal"] for row in attempts]
            if ordinals != list(range(len(ordinals))):
                raise ContractValidationError(
                    "transport retry ordinals must be contiguous and start at zero"
                )
            if len(ordinals) > role["transport_retry"]["max_retries"] + 1:
                raise ContractValidationError(
                    "transport attempts exceed the sealed retry cap"
                )
            for prior in attempts[:-1]:
                error = errors_by_id.get(prior["error_id"])
                if error is None or error["retry_disposition"] != "transport_retry_allowed":
                    raise ContractValidationError(
                        "transport retry lacks an allowed predecessor error"
                    )

        for prior_semantic_index in semantic_indexes[:-1]:
            prior = max(
                semantic_groups[prior_semantic_index],
                key=lambda row: row["transport_retry_ordinal"],
            )
            error = errors_by_id.get(prior["error_id"])
            if error is None or error["retry_disposition"] != "semantic_retry_allowed":
                raise ContractValidationError(
                    "semantic retry lacks an allowed predecessor error"
                )


def usage_limits_are_certifiable(
    *,
    usage_rows: Sequence[Mapping[str, Any]],
    role: Mapping[str, Any],
) -> bool:
    """Return whether sealed aggregate limits cover all physical attempts.

    Provider-unknown facts remain null in evidence. For a failed request that
    may have reached the provider, token accounting instead reserves the
    sealed per-call maxima. This permits an explicit retry only when its
    aggregate token budget was provisioned for the worst case. Cost remains
    uncertifiable without an observed or pinned monetary fact.
    """

    totals = _usage_limit_totals(usage_rows, role["generation"])
    limits = role["limits"]
    for field, limit_field in (
        ("prompt_tokens", "max_prompt_tokens"),
        ("completion_tokens", "max_completion_tokens"),
        ("total_tokens", "max_total_tokens"),
        ("cost_usd", "max_cost_usd"),
    ):
        cap = limits[limit_field]
        if cap is not None and (totals[field] is None or totals[field] > cap):
            return False
    return True


def _validate_usage_totals(
    usage: Sequence[Mapping[str, Any]], role: Mapping[str, Any]
) -> None:
    limits = role["limits"]
    totals = _usage_limit_totals(usage, role["generation"])
    for field, limit_field in (
        ("prompt_tokens", "max_prompt_tokens"),
        ("completion_tokens", "max_completion_tokens"),
        ("total_tokens", "max_total_tokens"),
    ):
        cap = limits[limit_field]
        if cap is not None:
            if totals[field] is None:
                raise ContractValidationError(
                    f"usage cannot certify sealed {limit_field} with unknown facts"
                )
            if totals[field] > cap:
                raise ContractValidationError(f"usage exceeds sealed {limit_field}")
    if limits["max_cost_usd"] is not None:
        if totals["cost_usd"] is None:
            raise ContractValidationError(
                "usage cannot certify sealed max_cost_usd with unknown cost"
            )
        if totals["cost_usd"] > limits["max_cost_usd"]:
            raise ContractValidationError("usage exceeds sealed max_cost_usd")


def _usage_limit_totals(
    usage: Sequence[Mapping[str, Any]],
    generation: Mapping[str, Any],
) -> dict[str, int | float | None]:
    provider_attempts = [
        row for row in usage if row["outcome"] != "failed_before_request"
    ]
    totals: dict[str, int | float | None] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"):
        facts = [
            _usage_limit_fact(row=row, field=field, generation=generation)
            for row in provider_attempts
        ]
        if any(fact is None for fact in facts):
            totals[field] = None
        else:
            totals[field] = sum(fact for fact in facts if fact is not None)
    return totals


def _usage_limit_fact(
    *,
    row: Mapping[str, Any],
    field: str,
    generation: Mapping[str, Any],
) -> int | float | None:
    fact = row[field]
    if fact is not None or row["outcome"] != "failed_after_request":
        return fact
    if field == "prompt_tokens":
        return generation["max_input_tokens"]
    if field == "completion_tokens":
        return generation["max_output_tokens"]
    if field == "total_tokens":
        prompt = _usage_limit_fact(
            row=row, field="prompt_tokens", generation=generation
        )
        completion = _usage_limit_fact(
            row=row, field="completion_tokens", generation=generation
        )
        if prompt is None or completion is None:
            return None
        return prompt + completion
    return None


def _validate_error_retry_against_seal(
    error: Mapping[str, Any], role: Mapping[str, Any]
) -> None:
    disposition = error["retry_disposition"]
    if disposition == "do_not_retry":
        return
    if disposition == "transport_retry_allowed":
        policy = role["transport_retry"]
        if policy["max_retries"] <= 0 or error["retry_class"] not in policy[
            "retryable_codes"
        ]:
            raise ContractValidationError(
                "transport retry disposition differs from the sealed retry policy"
            )
        return
    policy = role["semantic_retry"]
    if policy["max_retries"] <= 0 or error["retry_class"] not in policy[
        "retryable_categories"
    ]:
        raise ContractValidationError(
            "semantic retry disposition differs from the sealed retry policy"
        )


def _source_catalog(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ContractValidationError("api_sources must be a sequence")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    physical_buckets: dict[tuple[Any, ...], str] = {}
    for raw in rows:
        source = validate_api_source(raw)
        key = (source["source_id"], source["source_revision"])
        if key in result:
            raise ContractValidationError("api_sources repeats a source revision")
        physical_identity = (
            source["route_id"],
            source["endpoint_class"],
            source["base_url"],
            source["credential_commitment"],
        )
        prior_bucket = physical_buckets.get(physical_identity)
        if prior_bucket is not None and prior_bucket != source[
            "physical_quota_bucket_id"
        ]:
            raise ContractValidationError(
                "one physical source identity may not claim multiple quota buckets"
            )
        physical_buckets[physical_identity] = source["physical_quota_bucket_id"]
        result[key] = source
    if not result:
        raise ContractValidationError("api_sources is empty")
    return result


def _capability_catalog(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ContractValidationError("capability_evidence must be a sequence")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        capability = validate_capability_evidence(raw)
        key = (capability["capability_id"], capability["capability_revision"])
        if key in result:
            raise ContractValidationError("capability_evidence repeats a revision")
        result[key] = capability
    if not result:
        raise ContractValidationError("capability_evidence is empty")
    return result


def _resolve_target(
    target: Mapping[str, Any],
    *,
    role: Mapping[str, Any],
    source_catalog: Mapping[tuple[str, str], Mapping[str, Any]],
    capability_catalog: Mapping[tuple[str, str], Mapping[str, Any]],
    label: str,
) -> dict[str, Any]:
    source = source_catalog.get((target["source_id"], target["source_revision"]))
    if source is None:
        if any(key[0] == target["source_id"] for key in source_catalog):
            raise ContractValidationError(f"{label} source revision mismatch")
        raise ContractValidationError(f"{label} source is absent")
    capability = capability_catalog.get(
        (target["capability_id"], target["capability_revision"])
    )
    if capability is None:
        if any(key[0] == target["capability_id"] for key in capability_catalog):
            raise ContractValidationError(f"{label} capability revision mismatch")
        raise ContractValidationError(f"{label} capability evidence is absent")
    resolved = {
        "target": deepcopy(dict(target)),
        "source": deepcopy(dict(source)),
        "capability": deepcopy(dict(capability)),
    }
    return _validate_resolved_target(resolved, target=target, role=role, label=label)


def _validate_resolved_target(
    value: Any,
    *,
    target: Mapping[str, Any],
    role: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    resolved = _closed_object(
        value, {"target", "source", "capability"}, f"resolved {label}"
    )
    if resolved["target"] != target:
        raise ContractValidationError(f"resolved {label} target differs from profile")
    source = validate_api_source(resolved["source"])
    capability = validate_capability_evidence(resolved["capability"])
    if role["workstream"] == "evaluation":
        _reject_evaluation_authority_values(source, f"resolved {label} source")
        _reject_evaluation_authority_values(
            capability, f"resolved {label} capability"
        )
    if not source["enabled"]:
        raise ContractValidationError(f"resolved {label} source is disabled")
    if source["source_id"] != target["source_id"] or source[
        "source_revision"
    ] != target["source_revision"]:
        raise ContractValidationError(f"resolved {label} source revision mismatch")
    if capability["capability_id"] != target["capability_id"] or capability[
        "capability_revision"
    ] != target["capability_revision"]:
        raise ContractValidationError(f"resolved {label} capability revision mismatch")
    for field in (
        "source_id",
        "source_revision",
        "adapter_id",
        "protocol",
        "route_id",
        "base_url",
    ):
        if capability[field] != source[field]:
            raise ContractValidationError(
                f"resolved {label} capability {field} differs from source"
            )
    if capability["requested_model_id"] != target["requested_model_id"]:
        raise ContractValidationError(f"resolved {label} requested model mismatch")
    if capability["verdict"] != "qualified":
        raise ContractValidationError(f"resolved {label} capability is not qualified")

    mode = role["structured_output"]["mode"]
    if mode == "required" and capability["capability_kind"] != "native_structured_output":
        raise ContractValidationError(
            f"resolved {label} lacks required native structured-output authority"
        )
    if mode == "preferred" and capability["capability_kind"] not in {
        "native_structured_output",
        "json_object",
    }:
        raise ContractValidationError(
            f"resolved {label} lacks a qualified structured-output path"
        )
    if mode == "prompt_validated" and capability["capability_kind"] != "json_object":
        raise ContractValidationError(
            f"resolved {label} lacks a prompt-validated JSON response path"
        )
    if mode != "disabled" and capability["capability_kind"] in {
        "native_structured_output",
        "json_object",
    }:
        response_schema = role["response_schema"]
        if response_schema is None:
            raise ContractValidationError(
                f"resolved {label} structured capability lacks profile response schema"
            )
        if capability["schema_dialect"] != role["structured_output"]["schema_dialect"]:
            raise ContractValidationError(
                f"resolved {label} structured-output dialect mismatch"
            )
        if capability["schema_sha256"] != response_schema["sha256"]:
            raise ContractValidationError(
                f"resolved {label} structured-output schema mismatch"
            )
        if capability["local_validator_id"] != role["validator"]["id"] or capability[
            "local_validator_sha256"
        ] != role["validator"]["sha256"]:
            raise ContractValidationError(
                f"resolved {label} local validator binding mismatch"
            )
    if canonical_sha256(source) != target["source_record_sha256"]:
        raise ContractValidationError(f"resolved {label} source record hash mismatch")
    if canonical_sha256(capability) != target["capability_record_sha256"]:
        raise ContractValidationError(
            f"resolved {label} capability record hash mismatch"
        )
    resolved["source"] = source
    resolved["capability"] = capability
    resolved["target"] = deepcopy(dict(target))
    return resolved


def _reject_evaluation_authority_values(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_evaluation_authority_values(item, f"{label}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _reject_evaluation_authority_values(item, f"{label}[{index}]")
    elif isinstance(value, str):
        _reject_evaluation_authority_identifier(value, label)


def _namespace_material_sha256(
    profile_sha256: str,
    role_sha256: str,
    input_bindings_sha256: str,
    stage_id: str,
) -> str:
    return canonical_sha256(
        {
            "profile_sha256": _sha256(profile_sha256, "profile_sha256"),
            "role_binding_sha256": _sha256(role_sha256, "role_binding_sha256"),
            "input_bindings_sha256": _sha256(
                input_bindings_sha256, "input_bindings_sha256"
            ),
            "stage_id": _identifier(stage_id, "stage_id"),
        }
    )


def _resolved_namespaces(
    namespaces: Mapping[str, str],
    *,
    namespace_material_sha256: str,
    run_id: str,
    attempt_run_id: str,
) -> dict[str, str]:
    material = _sha256(namespace_material_sha256, "namespace_material_sha256")
    run_attempt_sha256 = canonical_sha256(
        {
            "run_id": _identifier(run_id, "run_id"),
            "attempt_run_id": _identifier(attempt_run_id, "attempt_run_id"),
        }
    )
    return {
        "output_root_id": _namespace_identifier(
            f"{namespaces['output']}.{material}.{run_attempt_sha256}",
            "output_root_id",
        ),
        "checkpoint_namespace": _namespace_identifier(
            f"{namespaces['checkpoint']}.{material}.{run_attempt_sha256}",
            "checkpoint_namespace",
        ),
        "cache_namespace": _namespace_identifier(
            f"{namespaces['cache']}.{material}", "cache_namespace"
        ),
    }


def _validate_input_bindings(
    value: Any, *, workstream: str | None = None
) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ContractValidationError("input_bindings must be a nonempty ordered sequence")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, start=1):
        row = _closed_object(raw, {"name", "sha256"}, f"input binding {index}")
        row["name"] = _identifier(row["name"], f"input binding {index} name")
        if workstream == "evaluation":
            _reject_evaluation_authority_identifier(
                row["name"], f"evaluation input binding {index}"
            )
        row["sha256"] = _sha256(row["sha256"], f"input binding {index} sha256")
        if row["name"] in seen:
            raise ContractValidationError("input_bindings repeats a name")
        seen.add(row["name"])
        result.append(row)
    return result
