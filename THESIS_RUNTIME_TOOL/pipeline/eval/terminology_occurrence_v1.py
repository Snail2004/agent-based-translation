from __future__ import annotations

import copy
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
import unicodedata
import uuid

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_number,
    require_rfc3339,
    require_sha256,
    require_string,
    seal_payload,
    validate_method,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.d2l_input_v1 import validate_d2l_evaluation_input


__all__ = [
    "TERMINOLOGY_OCCURRENCE_SCHEMA_ID",
    "TERMINOLOGY_OCCURRENCE_SCHEMA_VERSION",
    "TerminologyOccurrenceObservationV1",
    "TerminologyOccurrencePersistResultV1",
    "build_terminology_occurrence_metrics_v1",
    "persist_terminology_occurrence_metrics_v1",
    "project_d2l_cascade_occurrences_v1",
    "project_full_run_metric_rows_v1",
    "seal_terminology_occurrence_metrics_v1",
    "validate_terminology_occurrence_metrics_v1",
]


TERMINOLOGY_OCCURRENCE_SCHEMA_ID = "TerminologyOccurrenceMetricsV1"
TERMINOLOGY_OCCURRENCE_SCHEMA_VERSION = "1.0.0"
TC_OCC_METHOD_ID = "tc_occ"
TA_OCC_METHOD_ID = "ta_occ"
TC_OCC_METHOD_VERSION = "majority_rendering_per_occurrence_v1"
TA_OCC_METHOD_VERSION = "accepted_rendering_per_occurrence_v1"
RULER_KIND = "embedded_runtime_glossary_v1"
_SELF_HASH_PATH = ("integrity", "artifact_sha256")


@dataclass(frozen=True, slots=True)
class TerminologyOccurrenceObservationV1:
    arm_id: str
    occurrence_id: str
    source_occurrence_id: str
    block_id: str
    chapter_id: str
    term_id: str
    source_term: str
    source_start: int
    source_end: int
    source_surface: str
    source_text: str
    target_text: str
    localization_status: str
    rendered_surface: str | None
    adherence_status: str
    accepted_forms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TerminologyOccurrencePersistResultV1:
    path: Path
    artifact: dict[str, Any]
    reused: bool


def project_d2l_cascade_occurrences_v1(
    payload: Mapping[str, Any],
) -> tuple[TerminologyOccurrenceObservationV1, ...]:
    """Project one historical D2L cascade artifact into a typed read model.

    The adapter intentionally reads only the fields that carried the official
    TC-Occ/TA-Occ measurement. Extra legacy diagnostics remain outside the new
    contract and cannot affect the score.
    """

    root = require_mapping(payload, path="$cascade")
    arm_id = require_string(root.get("config"), path="$cascade.config")
    raw_rows = require_list(root.get("decisions"), path="$cascade.decisions")
    if not raw_rows:
        raise ContractValidationError(
            "empty_array", "$cascade.decisions", "occurrence decisions are required"
        )

    result: list[TerminologyOccurrenceObservationV1] = []
    seen_occurrence_ids: set[str] = set()
    seen_source_ids: set[str] = set()
    for index, raw in enumerate(raw_rows):
        path = f"$cascade.decisions[{index}]"
        row = require_mapping(raw, path=path)
        row_arm = require_string(row.get("config"), path=f"{path}.config")
        if row_arm != arm_id:
            raise ContractValidationError(
                "arm_binding", f"{path}.config", "decision arm differs from artifact arm"
            )
        occurrence_id = require_string(row.get("occ_id"), path=f"{path}.occ_id")
        if occurrence_id in seen_occurrence_ids:
            raise ContractValidationError(
                "duplicate", f"{path}.occ_id", "occurrence ID is duplicated"
            )
        seen_occurrence_ids.add(occurrence_id)

        source_text = require_string(row.get("source_text"), path=f"{path}.source_text")
        source_start = require_int(
            row.get("source_start"), path=f"{path}.source_start", minimum=0
        )
        source_end = require_int(
            row.get("source_end"), path=f"{path}.source_end", minimum=0
        )
        source_surface = require_string(
            row.get("source_surface"), path=f"{path}.source_surface"
        )
        if source_end <= source_start or source_end > len(source_text):
            raise ContractValidationError(
                "source_span", path, "source span is outside source text"
            )
        if source_text[source_start:source_end] != source_surface:
            raise ContractValidationError(
                "source_span", path, "source surface does not match source coordinates"
            )

        block_id = require_string(row.get("block_id"), path=f"{path}.block_id")
        chapter_id = require_string(row.get("chapter_id"), path=f"{path}.chapter_id")
        term_id = require_string(row.get("term_id"), path=f"{path}.term_id")
        source_term = require_string(row.get("source_term"), path=f"{path}.source_term")
        target_text = require_string(row.get("target_text"), path=f"{path}.target_text")
        source_occurrence_id = _source_occurrence_id(
            block_id=block_id,
            term_id=term_id,
            source_start=source_start,
            source_end=source_end,
            source_surface=source_surface,
        )
        if source_occurrence_id in seen_source_ids:
            raise ContractValidationError(
                "duplicate", path, "source occurrence coordinates are duplicated"
            )
        seen_source_ids.add(source_occurrence_id)

        accepted_forms = _string_tuple(
            row.get("accepted_forms", []), path=f"{path}.accepted_forms"
        )
        decision = require_string(row.get("decision"), path=f"{path}.decision")
        resolved_by = require_string(row.get("resolved_by"), path=f"{path}.resolved_by")
        rendered_surface = _rendered_surface(row)
        if decision == "not_rendered":
            localization_status = "not_rendered"
            rendered_surface = None
        elif rendered_surface is None:
            localization_status = "unresolved"
        else:
            localization_status = "localized"

        t3_score = row.get("t3_code_score")
        if t3_score is None:
            t3_label = None
        else:
            t3_label = require_string(
                require_mapping(t3_score, path=f"{path}.t3_code_score").get(
                    "adherence_label"
                ),
                path=f"{path}.t3_code_score.adherence_label",
            )
            if t3_label not in {"adherent", "off_glossary", "not_rendered"}:
                raise ContractValidationError(
                    "enum",
                    f"{path}.t3_code_score.adherence_label",
                    "unsupported adherence label",
                )
        if resolved_by == "t2_credit" and decision == "rendered":
            adherence_status = "adherent"
        elif t3_label == "adherent":
            adherence_status = "adherent"
        elif localization_status == "not_rendered":
            adherence_status = "not_rendered"
        elif t3_label == "off_glossary":
            adherence_status = "off_glossary"
        else:
            adherence_status = "unresolved"

        if adherence_status == "adherent" and localization_status != "localized":
            raise ContractValidationError(
                "adherence_binding",
                path,
                "adherent occurrence must have a localized rendering",
            )
        _validate_target_span(row, target_text=target_text, path=path)
        result.append(
            TerminologyOccurrenceObservationV1(
                arm_id=arm_id,
                occurrence_id=occurrence_id,
                source_occurrence_id=source_occurrence_id,
                block_id=block_id,
                chapter_id=chapter_id,
                term_id=term_id,
                source_term=source_term,
                source_start=source_start,
                source_end=source_end,
                source_surface=source_surface,
                source_text=source_text,
                target_text=target_text,
                localization_status=localization_status,
                rendered_surface=rendered_surface,
                adherence_status=adherence_status,
                accepted_forms=accepted_forms,
            )
        )
    return tuple(result)


def build_terminology_occurrence_metrics_v1(
    d2l_package: Mapping[str, Any],
    cascade_payloads: Mapping[str, Mapping[str, Any]],
    cascade_artifacts: Mapping[str, Mapping[str, Any]],
    *,
    generated_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    """Build deterministic D2L-profile TC-Occ and TA-Occ evidence."""

    package = validate_d2l_evaluation_input(d2l_package)
    timestamp = require_rfc3339(generated_at, path="$.generated_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    package_arms = {row["arm_id"]: row for row in package["arms"]}
    if set(cascade_payloads) != set(package_arms):
        raise ContractValidationError(
            "arm_exact_cover",
            "$.cascade_payloads",
            "cascade payloads must exact-cover package arms",
        )
    if set(cascade_artifacts) != set(package_arms):
        raise ContractValidationError(
            "artifact_exact_cover",
            "$.cascade_artifacts",
            "cascade artifacts must exact-cover package arms",
        )

    observations: dict[str, tuple[TerminologyOccurrenceObservationV1, ...]] = {}
    inputs: dict[str, dict[str, str]] = {}
    for arm_id in sorted(package_arms):
        rows = project_d2l_cascade_occurrences_v1(cascade_payloads[arm_id])
        if not rows or any(row.arm_id != arm_id for row in rows):
            raise ContractValidationError(
                "arm_binding", f"$.cascade_payloads.{arm_id}", "cascade arm mismatch"
            )
        _validate_package_binding(package, arm_id=arm_id, observations=rows)
        observations[arm_id] = rows
        descriptor = _validate_cascade_artifact_descriptor(
            cascade_artifacts[arm_id], path=f"$.cascade_artifacts.{arm_id}"
        )
        inputs[arm_id] = {
            **descriptor,
            "payload_sha256": _canonical_digest(cascade_payloads[arm_id]),
        }

    _validate_cross_arm_occurrences(observations)
    ruler_sha256 = _ruler_sha256(observations)
    arm_summaries = {
        arm_id: _score_arm(rows) for arm_id, rows in sorted(observations.items())
    }
    baseline_arm_id = _arm_with_role(package, "baseline")
    candidate_arm_id = _arm_with_role(package, "candidate")
    comparison = _comparison(
        arm_summaries,
        baseline_arm_id=baseline_arm_id,
        candidate_arm_id=candidate_arm_id,
    )
    source_artifact_ids = sorted(row["artifact_id"] for row in inputs.values())
    identity = package["identity"]
    profile = package["runtime_profile"]
    artifact_seed = {
        "source_package_sha256": package["integrity"]["package_sha256"],
        "profile_id": profile["profile_id"],
        "inputs": inputs,
        "ruler_sha256": ruler_sha256,
    }
    artifact_id = "terminology-occurrence-" + _canonical_digest(artifact_seed)[:24]
    draft = {
        "schema_id": TERMINOLOGY_OCCURRENCE_SCHEMA_ID,
        "schema_version": TERMINOLOGY_OCCURRENCE_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "generated_at": timestamp,
        "producer": {
            "workstream": "evaluation",
            "component": "terminology_occurrence_v1",
            "component_version": "1.0.0",
            "code_commit": commit,
        },
        "identity": {
            "project_id": identity["project_id"],
            "document_id": identity["document_id"],
            "logical_run_id": identity["logical_run_id"],
            "experiment_id": identity["experiment_id"],
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "selected_chapter_ids": list(identity["selected_chapter_ids"]),
            "source_package_sha256": package["integrity"]["package_sha256"],
        },
        "inputs": inputs,
        "ruler": {
            "ruler_id": "runtime-glossary-embedded-" + ruler_sha256[:24],
            "ruler_kind": RULER_KIND,
            "ruler_sha256": ruler_sha256,
            "source_artifact_ids": source_artifact_ids,
        },
        "methods": {
            TC_OCC_METHOD_ID: {
                "method_id": TC_OCC_METHOD_ID,
                "method_version": TC_OCC_METHOD_VERSION,
                "implementation_commit": commit,
                "prompt_version": None,
                "model_id": None,
            },
            TA_OCC_METHOD_ID: {
                "method_id": TA_OCC_METHOD_ID,
                "method_version": TA_OCC_METHOD_VERSION,
                "implementation_commit": commit,
                "prompt_version": None,
                "model_id": None,
            },
        },
        "arms": arm_summaries,
        "comparison": comparison,
        "caveats": [
            "TC-Occ measures consistency and does not establish terminology correctness.",
            "TA-Occ uses the embedded runtime glossary ruler and is not an external-reference score.",
            "Unresolved adherence receives no conservative headline credit and remains disclosed in the upper bound.",
        ],
        "integrity": {"artifact_sha256": "0" * 64},
    }
    return validate_terminology_occurrence_metrics_v1(
        seal_terminology_occurrence_metrics_v1(draft)
    )


def seal_terminology_occurrence_metrics_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return seal_payload(
        payload, policy=_canonical_policy(payload), hash_path=_SELF_HASH_PATH
    )


def validate_terminology_occurrence_metrics_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "artifact_id",
            "generated_at",
            "producer",
            "identity",
            "inputs",
            "ruler",
            "methods",
            "arms",
            "comparison",
            "caveats",
            "integrity",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {TERMINOLOGY_OCCURRENCE_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {TERMINOLOGY_OCCURRENCE_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "artifact_id": require_string(root["artifact_id"], path="$.artifact_id"),
        "generated_at": require_rfc3339(root["generated_at"], path="$.generated_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "identity": _validate_identity(root["identity"]),
        "inputs": _validate_inputs(root["inputs"]),
        "ruler": _validate_ruler(root["ruler"]),
        "methods": _validate_methods(root["methods"]),
        "arms": _validate_arm_summaries(root["arms"]),
        "comparison": _validate_comparison(root["comparison"]),
        "caveats": _string_list(root["caveats"], path="$.caveats"),
        "integrity": _validate_integrity(root["integrity"]),
    }
    if set(normalized["inputs"]) != set(normalized["arms"]):
        raise ContractValidationError(
            "arm_exact_cover", "$.inputs", "input artifacts must exact-cover metric arms"
        )
    input_ids = {row["artifact_id"] for row in normalized["inputs"].values()}
    if set(normalized["ruler"]["source_artifact_ids"]) != input_ids:
        raise ContractValidationError(
            "ruler_provenance",
            "$.ruler.source_artifact_ids",
            "embedded ruler must reference the exact occurrence artifact set",
        )
    _validate_comparison_against_arms(normalized["comparison"], normalized["arms"])
    seed = {
        "source_package_sha256": normalized["identity"]["source_package_sha256"],
        "profile_id": normalized["identity"]["profile_id"],
        "inputs": normalized["inputs"],
        "ruler_sha256": normalized["ruler"]["ruler_sha256"],
    }
    expected_id = "terminology-occurrence-" + _canonical_digest(seed)[:24]
    if normalized["artifact_id"] != expected_id:
        raise ContractValidationError(
            "artifact_id", "$.artifact_id", "artifact ID differs from bound inputs"
        )
    policy = _canonical_policy(normalized)
    if not verify_payload_hash(normalized, policy=policy, hash_path=_SELF_HASH_PATH):
        raise ContractValidationError(
            "artifact_hash", "$.integrity.artifact_sha256", "self-hash mismatch"
        )
    canonical = canonicalize(normalized, policy=policy)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical terminology occurrence artifact must be an object")
    return canonical


def persist_terminology_occurrence_metrics_v1(
    *, output_root: Path, artifact_payload: Mapping[str, Any]
) -> TerminologyOccurrencePersistResultV1:
    artifact = validate_terminology_occurrence_metrics_v1(artifact_payload)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    relative = (
        "technical_metrics/terminology_occurrence/"
        f"{artifact['integrity']['artifact_sha256']}.json"
    )
    path = _contained_path(root, relative)
    encoded = _canonical_json_bytes(artifact)
    reused = not _publish_bytes_create_only(path, encoded)
    return TerminologyOccurrencePersistResultV1(
        path=path, artifact=artifact, reused=reused
    )


def project_full_run_metric_rows_v1(
    artifact_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project sidecar results into FullRunReportV1-compatible metric rows.

    This function does not mutate or publish a FullRunReport. It is the bounded
    integration surface for a later public-report wiring task.
    """

    artifact = validate_terminology_occurrence_metrics_v1(artifact_payload)
    source_ids = sorted(row["artifact_id"] for row in artifact["inputs"].values())
    comparison = artifact["comparison"]
    result: list[dict[str, Any]] = []
    for metric_id, display_name in (
        (TC_OCC_METHOD_ID, "TC-Occ"),
        (TA_OCC_METHOD_ID, "TA-Occ"),
    ):
        arm_values = []
        for arm_id, summary in sorted(artifact["arms"].items()):
            metric = summary[metric_id]
            if metric_id == TC_OCC_METHOD_ID:
                numerator = metric["numerator_majority"]
                denominator = metric["denominator_localized"]
                value = metric["value"]
            else:
                numerator = metric["numerator_accepted"]
                denominator = metric["denominator_all_occurrences"]
                value = metric["value_lower"]
            arm_values.append(
                {
                    "arm_id": arm_id,
                    "value": value,
                    "numerator": numerator,
                    "denominator": denominator,
                    "interval_low": None,
                    "interval_high": None,
                    "interval_level": None,
                }
            )
        if comparison["status"] == "available":
            metric_comparison = {
                "status": "available",
                "baseline_arm_id": comparison["baseline_arm_id"],
                "candidate_arm_id": comparison["candidate_arm_id"],
                "delta": comparison[f"{metric_id}_delta"],
                "wins": None,
                "ties": None,
                "losses": None,
            }
        else:
            metric_comparison = {
                "status": "not_applicable",
                "baseline_arm_id": None,
                "candidate_arm_id": None,
                "delta": None,
                "wins": None,
                "ties": None,
                "losses": None,
            }
        caveats = list(artifact["caveats"])
        if metric_id == TA_OCC_METHOD_ID:
            caveats.append(
                "TA-Occ headline is the conservative lower value; possible upper values remain in the source artifact."
            )
        result.append(
            {
                "metric_id": metric_id,
                "display_name": display_name,
                "profile_scope": "d2l",
                "status": "available",
                "unit": "ratio",
                "direction": "higher_is_better",
                "method": copy.deepcopy(artifact["methods"][metric_id]),
                "arm_values": arm_values,
                "comparison": metric_comparison,
                "source_artifact_ids": source_ids,
                "caveats": caveats,
            }
        )
    return result


def _score_arm(
    rows: Sequence[TerminologyOccurrenceObservationV1],
) -> dict[str, Any]:
    tc_by_term: dict[str, Counter[str]] = defaultdict(Counter)
    source_term_by_id: dict[str, str] = {}
    localized_rows: list[TerminologyOccurrenceObservationV1] = []
    for row in rows:
        source_term_by_id.setdefault(row.term_id, row.source_term)
        if source_term_by_id[row.term_id] != row.source_term:
            raise ContractValidationError(
                "term_identity", "$.observations", "term ID maps to multiple source terms"
            )
        if row.localization_status != "localized" or row.rendered_surface is None:
            continue
        rendering = _norm(row.rendered_surface)
        if not rendering:
            continue
        localized_rows.append(row)
        tc_by_term[row.term_id][rendering] += 1

    tc_terms: dict[str, dict[str, Any]] = {}
    selected_by_term: dict[str, str] = {}
    tc_numerator = 0
    tied_terms = 0
    for term_id, counts in sorted(tc_by_term.items()):
        maximum = max(counts.values())
        dominant = sorted(form for form, count in counts.items() if count == maximum)
        selected = dominant[0]
        selected_by_term[term_id] = selected
        tc_numerator += maximum
        tied_terms += int(len(dominant) > 1)
        denominator = sum(counts.values())
        tc_terms[term_id] = {
            "source_term": source_term_by_id[term_id],
            "localized_occurrences": denominator,
            "numerator_majority": maximum,
            "value": _ratio(maximum, denominator),
            "dominant_renderings": dominant,
            "selected_dominant_rendering": selected,
            "tied_maximum": len(dominant) > 1,
        }
    tc_denominator = len(localized_rows)
    tc_violation_blocks = sorted(
        {
            row.block_id
            for row in localized_rows
            if _norm(row.rendered_surface or "") != selected_by_term[row.term_id]
        }
    )

    accepted = sum(row.adherence_status == "adherent" for row in rows)
    unresolved = sum(row.adherence_status == "unresolved" for row in rows)
    total = len(rows)
    ta_violations = sorted(
        {row.block_id for row in rows if row.adherence_status != "adherent"}
    )
    return {
        "source_occurrence_count": total,
        "localized_occurrence_count": tc_denominator,
        "not_rendered_occurrence_count": sum(
            row.localization_status == "not_rendered" for row in rows
        ),
        "unresolved_localization_count": sum(
            row.localization_status == "unresolved" for row in rows
        ),
        "tc_occ": {
            "numerator_majority": tc_numerator,
            "denominator_localized": tc_denominator,
            "value": _ratio(tc_numerator, tc_denominator),
            "term_count": len(tc_terms),
            "tied_term_count": tied_terms,
            "violation_block_ids": tc_violation_blocks,
            "terms": tc_terms,
        },
        "ta_occ": {
            "numerator_accepted": accepted,
            "denominator_all_occurrences": total,
            "value_lower": _ratio(accepted, total),
            "possible_upper_numerator": accepted + unresolved,
            "value_upper": _ratio(accepted + unresolved, total),
            "unresolved_adherence_count": unresolved,
            "violation_block_ids": ta_violations,
        },
    }


def _validate_package_binding(
    package: Mapping[str, Any],
    *,
    arm_id: str,
    observations: Sequence[TerminologyOccurrenceObservationV1],
) -> None:
    blocks = {row["block_id"]: row for row in package["blocks"]}
    translations = {
        (row["arm_id"], row["block_id"]): row for row in package["translations"]
    }
    for index, observation in enumerate(observations):
        path = f"$.cascade_payloads.{arm_id}.decisions[{index}]"
        block = blocks.get(observation.block_id)
        if block is None or block["chapter_id"] != observation.chapter_id:
            raise ContractValidationError(
                "block_binding", path, "occurrence references a foreign source block"
            )
        if block["source_text"] != observation.source_text:
            raise ContractValidationError(
                "source_text_binding", path, "occurrence source text differs from package"
            )
        translation = translations.get((arm_id, observation.block_id))
        if translation is None or translation["target_text"] != observation.target_text:
            raise ContractValidationError(
                "target_text_binding", path, "occurrence target text differs from package"
            )


def _validate_cross_arm_occurrences(
    observations: Mapping[str, Sequence[TerminologyOccurrenceObservationV1]],
) -> None:
    arm_ids = sorted(observations)
    reference_arm = arm_ids[0]
    reference = {row.source_occurrence_id: row for row in observations[reference_arm]}
    for arm_id in arm_ids[1:]:
        current = {row.source_occurrence_id: row for row in observations[arm_id]}
        if set(current) != set(reference):
            raise ContractValidationError(
                "occurrence_exact_cover",
                f"$.cascade_payloads.{arm_id}",
                "arms must exact-cover the same source occurrence universe",
            )
        for source_id, reference_row in reference.items():
            row = current[source_id]
            if (
                row.block_id,
                row.chapter_id,
                row.term_id,
                row.source_term,
                row.source_start,
                row.source_end,
                row.source_surface,
            ) != (
                reference_row.block_id,
                reference_row.chapter_id,
                reference_row.term_id,
                reference_row.source_term,
                reference_row.source_start,
                reference_row.source_end,
                reference_row.source_surface,
            ):
                raise ContractValidationError(
                    "occurrence_identity",
                    f"$.cascade_payloads.{arm_id}",
                    "source occurrence identity differs across arms",
                )
            if _normalized_forms(row.accepted_forms) != _normalized_forms(
                reference_row.accepted_forms
            ):
                raise ContractValidationError(
                    "ruler_drift",
                    f"$.cascade_payloads.{arm_id}",
                    "accepted-form ruler differs across arms",
                )


def _ruler_sha256(
    observations: Mapping[str, Sequence[TerminologyOccurrenceObservationV1]],
) -> str:
    first_arm = sorted(observations)[0]
    rows = [
        {
            "source_occurrence_id": row.source_occurrence_id,
            "accepted_forms": list(_normalized_forms(row.accepted_forms)),
        }
        for row in sorted(
            observations[first_arm], key=lambda item: item.source_occurrence_id
        )
    ]
    return _canonical_digest({"rows": rows})


def _comparison(
    arms: Mapping[str, Mapping[str, Any]],
    *,
    baseline_arm_id: str | None,
    candidate_arm_id: str | None,
) -> dict[str, Any]:
    if baseline_arm_id is None or candidate_arm_id is None:
        return {
            "status": "not_applicable",
            "baseline_arm_id": None,
            "candidate_arm_id": None,
            "tc_occ_delta": None,
            "ta_occ_delta": None,
        }
    return {
        "status": "available",
        "baseline_arm_id": baseline_arm_id,
        "candidate_arm_id": candidate_arm_id,
        "tc_occ_delta": arms[candidate_arm_id]["tc_occ"]["value"]
        - arms[baseline_arm_id]["tc_occ"]["value"],
        "ta_occ_delta": arms[candidate_arm_id]["ta_occ"]["value_lower"]
        - arms[baseline_arm_id]["ta_occ"]["value_lower"],
    }


def _arm_with_role(package: Mapping[str, Any], role: str) -> str | None:
    matches = [row["arm_id"] for row in package["arms"] if row["role"] == role]
    return matches[0] if matches else None


def _validate_identity(value: Any) -> dict[str, Any]:
    path = "$.identity"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "project_id",
            "document_id",
            "logical_run_id",
            "experiment_id",
            "profile_id",
            "profile_version",
            "selected_chapter_ids",
            "source_package_sha256",
        },
        path=path,
    )
    chapters = _string_list(
        row["selected_chapter_ids"], path=f"{path}.selected_chapter_ids"
    )
    if not chapters or len(chapters) != len(set(chapters)):
        raise ContractValidationError(
            "chapter_identity", f"{path}.selected_chapter_ids", "chapters must be unique"
        )
    return {
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
        "logical_run_id": require_string(
            row["logical_run_id"], path=f"{path}.logical_run_id"
        ),
        "experiment_id": require_string(
            row["experiment_id"], path=f"{path}.experiment_id"
        ),
        "profile_id": require_string(row["profile_id"], path=f"{path}.profile_id"),
        "profile_version": require_string(
            row["profile_version"], path=f"{path}.profile_version"
        ),
        "selected_chapter_ids": chapters,
        "source_package_sha256": require_sha256(
            row["source_package_sha256"], path=f"{path}.source_package_sha256"
        ),
    }


def _validate_inputs(value: Any) -> dict[str, dict[str, str]]:
    path = "$.inputs"
    rows = require_mapping(value, path=path)
    if not rows:
        raise ContractValidationError("empty_object", path, "input artifacts are required")
    result: dict[str, dict[str, str]] = {}
    artifact_ids: set[str] = set()
    for arm_id, raw in rows.items():
        if not isinstance(arm_id, str) or not arm_id:
            raise ContractValidationError("arm_id", path, "arm keys must be strings")
        item = _validate_input_artifact(raw, path=f"{path}.{arm_id}")
        if item["artifact_id"] in artifact_ids:
            raise ContractValidationError(
                "duplicate", f"{path}.{arm_id}.artifact_id", "artifact ID is duplicated"
            )
        artifact_ids.add(item["artifact_id"])
        result[arm_id] = item
    return result


def _validate_input_artifact(value: Any, *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"artifact_id", "artifact_sha256", "payload_sha256"},
        path=path,
    )
    return {
        "artifact_id": require_string(row["artifact_id"], path=f"{path}.artifact_id"),
        "artifact_sha256": require_sha256(
            row["artifact_sha256"], path=f"{path}.artifact_sha256"
        ),
        "payload_sha256": require_sha256(
            row["payload_sha256"], path=f"{path}.payload_sha256"
        ),
    }


def _validate_cascade_artifact_descriptor(
    value: Any, *, path: str
) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"artifact_id", "artifact_sha256"}, path=path)
    return {
        "artifact_id": require_string(row["artifact_id"], path=f"{path}.artifact_id"),
        "artifact_sha256": require_sha256(
            row["artifact_sha256"], path=f"{path}.artifact_sha256"
        ),
    }


def _validate_ruler(value: Any) -> dict[str, Any]:
    path = "$.ruler"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"ruler_id", "ruler_kind", "ruler_sha256", "source_artifact_ids"},
        path=path,
    )
    ids = _string_list(row["source_artifact_ids"], path=f"{path}.source_artifact_ids")
    if len(ids) != len(set(ids)):
        raise ContractValidationError(
            "duplicate", f"{path}.source_artifact_ids", "artifact IDs are duplicated"
        )
    return {
        "ruler_id": require_string(row["ruler_id"], path=f"{path}.ruler_id"),
        "ruler_kind": require_enum(
            row["ruler_kind"], {RULER_KIND}, path=f"{path}.ruler_kind"
        ),
        "ruler_sha256": require_sha256(
            row["ruler_sha256"], path=f"{path}.ruler_sha256"
        ),
        "source_artifact_ids": ids,
    }


def _validate_methods(value: Any) -> dict[str, dict[str, Any]]:
    path = "$.methods"
    rows = require_mapping(value, path=path)
    if set(rows) != {TC_OCC_METHOD_ID, TA_OCC_METHOD_ID}:
        raise ContractValidationError(
            "method_exact_cover", path, "methods must contain TC-Occ and TA-Occ only"
        )
    result = {
        method_id: validate_method(rows[method_id], path=f"{path}.{method_id}")
        for method_id in (TC_OCC_METHOD_ID, TA_OCC_METHOD_ID)
    }
    expected_versions = {
        TC_OCC_METHOD_ID: TC_OCC_METHOD_VERSION,
        TA_OCC_METHOD_ID: TA_OCC_METHOD_VERSION,
    }
    for method_id, row in result.items():
        if row["method_id"] != method_id or row["method_version"] != expected_versions[method_id]:
            raise ContractValidationError(
                "method_identity", f"{path}.{method_id}", "method identity is inconsistent"
            )
        if row["prompt_version"] is not None or row["model_id"] is not None:
            raise ContractValidationError(
                "deterministic_method",
                f"{path}.{method_id}",
                "deterministic occurrence metrics cannot claim prompt/model authority",
            )
    return result


def _validate_arm_summaries(value: Any) -> dict[str, dict[str, Any]]:
    path = "$.arms"
    rows = require_mapping(value, path=path)
    if not rows:
        raise ContractValidationError("empty_object", path, "arm summaries are required")
    return {
        arm_id: _validate_arm_summary(raw, path=f"{path}.{arm_id}")
        for arm_id, raw in rows.items()
    }


def _validate_arm_summary(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "source_occurrence_count",
            "localized_occurrence_count",
            "not_rendered_occurrence_count",
            "unresolved_localization_count",
            "tc_occ",
            "ta_occ",
        },
        path=path,
    )
    source_count = require_int(
        row["source_occurrence_count"], path=f"{path}.source_occurrence_count", minimum=1
    )
    localized = require_int(
        row["localized_occurrence_count"],
        path=f"{path}.localized_occurrence_count",
        minimum=0,
    )
    not_rendered = require_int(
        row["not_rendered_occurrence_count"],
        path=f"{path}.not_rendered_occurrence_count",
        minimum=0,
    )
    unresolved = require_int(
        row["unresolved_localization_count"],
        path=f"{path}.unresolved_localization_count",
        minimum=0,
    )
    if localized + not_rendered + unresolved != source_count:
        raise ContractValidationError(
            "localization_partition", path, "localization statuses must partition occurrences"
        )
    tc = _validate_tc(row["tc_occ"], path=f"{path}.tc_occ")
    ta = _validate_ta(row["ta_occ"], path=f"{path}.ta_occ")
    if tc["denominator_localized"] != localized or ta["denominator_all_occurrences"] != source_count:
        raise ContractValidationError(
            "metric_denominator", path, "metric denominators differ from arm coverage"
        )
    return {
        "source_occurrence_count": source_count,
        "localized_occurrence_count": localized,
        "not_rendered_occurrence_count": not_rendered,
        "unresolved_localization_count": unresolved,
        "tc_occ": tc,
        "ta_occ": ta,
    }


def _validate_tc(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "numerator_majority",
            "denominator_localized",
            "value",
            "term_count",
            "tied_term_count",
            "violation_block_ids",
            "terms",
        },
        path=path,
    )
    numerator = require_int(row["numerator_majority"], path=f"{path}.numerator_majority", minimum=0)
    denominator = require_int(
        row["denominator_localized"], path=f"{path}.denominator_localized", minimum=0
    )
    score = require_number(row["value"], path=f"{path}.value", minimum=0)
    if score > 1 or numerator > denominator or not _ratio_equal(score, numerator, denominator):
        raise ContractValidationError("metric_arithmetic", path, "TC-Occ arithmetic is inconsistent")
    terms = _validate_tc_terms(row["terms"], path=f"{path}.terms")
    term_count = require_int(row["term_count"], path=f"{path}.term_count", minimum=0)
    tied = require_int(row["tied_term_count"], path=f"{path}.tied_term_count", minimum=0)
    if term_count != len(terms):
        raise ContractValidationError("term_count", f"{path}.term_count", "term count mismatch")
    if sum(term["localized_occurrences"] for term in terms.values()) != denominator:
        raise ContractValidationError("term_total", f"{path}.terms", "term denominator mismatch")
    if sum(term["numerator_majority"] for term in terms.values()) != numerator:
        raise ContractValidationError("term_total", f"{path}.terms", "term numerator mismatch")
    if sum(bool(term["tied_maximum"]) for term in terms.values()) != tied:
        raise ContractValidationError("tie_count", f"{path}.tied_term_count", "tie count mismatch")
    violations = _unique_strings(row["violation_block_ids"], path=f"{path}.violation_block_ids")
    return {
        "numerator_majority": numerator,
        "denominator_localized": denominator,
        "value": score,
        "term_count": term_count,
        "tied_term_count": tied,
        "violation_block_ids": violations,
        "terms": terms,
    }


def _validate_tc_terms(value: Any, *, path: str) -> dict[str, dict[str, Any]]:
    rows = require_mapping(value, path=path)
    result: dict[str, dict[str, Any]] = {}
    for term_id, raw in rows.items():
        term_path = f"{path}.{term_id}"
        row = require_mapping(raw, path=term_path)
        require_exact_keys(
            row,
            required={
                "source_term",
                "localized_occurrences",
                "numerator_majority",
                "value",
                "dominant_renderings",
                "selected_dominant_rendering",
                "tied_maximum",
            },
            path=term_path,
        )
        denominator = require_int(
            row["localized_occurrences"], path=f"{term_path}.localized_occurrences", minimum=1
        )
        numerator = require_int(
            row["numerator_majority"], path=f"{term_path}.numerator_majority", minimum=1
        )
        score = require_number(row["value"], path=f"{term_path}.value", minimum=0)
        dominant = _unique_strings(
            row["dominant_renderings"], path=f"{term_path}.dominant_renderings"
        )
        selected = require_string(
            row["selected_dominant_rendering"],
            path=f"{term_path}.selected_dominant_rendering",
        )
        tied = row["tied_maximum"]
        if not isinstance(tied, bool):
            raise ContractValidationError("type", f"{term_path}.tied_maximum", "expected bool")
        if not dominant or selected not in dominant or tied != (len(dominant) > 1):
            raise ContractValidationError("dominant_rendering", term_path, "dominant rendering metadata is inconsistent")
        if numerator > denominator or not _ratio_equal(score, numerator, denominator):
            raise ContractValidationError("metric_arithmetic", term_path, "term TC arithmetic is inconsistent")
        result[require_string(term_id, path=term_path)] = {
            "source_term": require_string(row["source_term"], path=f"{term_path}.source_term"),
            "localized_occurrences": denominator,
            "numerator_majority": numerator,
            "value": score,
            "dominant_renderings": dominant,
            "selected_dominant_rendering": selected,
            "tied_maximum": tied,
        }
    return result


def _validate_ta(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "numerator_accepted",
            "denominator_all_occurrences",
            "value_lower",
            "possible_upper_numerator",
            "value_upper",
            "unresolved_adherence_count",
            "violation_block_ids",
        },
        path=path,
    )
    numerator = require_int(row["numerator_accepted"], path=f"{path}.numerator_accepted", minimum=0)
    denominator = require_int(
        row["denominator_all_occurrences"], path=f"{path}.denominator_all_occurrences", minimum=1
    )
    unresolved = require_int(
        row["unresolved_adherence_count"], path=f"{path}.unresolved_adherence_count", minimum=0
    )
    upper_numerator = require_int(
        row["possible_upper_numerator"], path=f"{path}.possible_upper_numerator", minimum=0
    )
    lower = require_number(row["value_lower"], path=f"{path}.value_lower", minimum=0)
    upper = require_number(row["value_upper"], path=f"{path}.value_upper", minimum=0)
    if (
        numerator + unresolved != upper_numerator
        or upper_numerator > denominator
        or upper > 1
        or lower > upper
        or not _ratio_equal(lower, numerator, denominator)
        or not _ratio_equal(upper, upper_numerator, denominator)
    ):
        raise ContractValidationError("metric_arithmetic", path, "TA-Occ arithmetic is inconsistent")
    return {
        "numerator_accepted": numerator,
        "denominator_all_occurrences": denominator,
        "value_lower": lower,
        "possible_upper_numerator": upper_numerator,
        "value_upper": upper,
        "unresolved_adherence_count": unresolved,
        "violation_block_ids": _unique_strings(
            row["violation_block_ids"], path=f"{path}.violation_block_ids"
        ),
    }


def _validate_comparison(value: Any) -> dict[str, Any]:
    path = "$.comparison"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "status",
            "baseline_arm_id",
            "candidate_arm_id",
            "tc_occ_delta",
            "ta_occ_delta",
        },
        path=path,
    )
    status = require_enum(row["status"], {"available", "not_applicable"}, path=f"{path}.status")
    result = {
        "status": status,
        "baseline_arm_id": row["baseline_arm_id"],
        "candidate_arm_id": row["candidate_arm_id"],
        "tc_occ_delta": row["tc_occ_delta"],
        "ta_occ_delta": row["ta_occ_delta"],
    }
    if status == "not_applicable":
        if any(value is not None for key, value in result.items() if key != "status"):
            raise ContractValidationError("comparison", path, "not-applicable comparison must be empty")
        return result
    result["baseline_arm_id"] = require_string(result["baseline_arm_id"], path=f"{path}.baseline_arm_id")
    result["candidate_arm_id"] = require_string(result["candidate_arm_id"], path=f"{path}.candidate_arm_id")
    result["tc_occ_delta"] = require_number(result["tc_occ_delta"], path=f"{path}.tc_occ_delta")
    result["ta_occ_delta"] = require_number(result["ta_occ_delta"], path=f"{path}.ta_occ_delta")
    return result


def _validate_comparison_against_arms(
    comparison: Mapping[str, Any], arms: Mapping[str, Mapping[str, Any]]
) -> None:
    if comparison["status"] == "not_applicable":
        if len(arms) != 1:
            raise ContractValidationError(
                "comparison", "$.comparison", "multi-arm artifact requires comparison roles"
            )
        return
    baseline = comparison["baseline_arm_id"]
    candidate = comparison["candidate_arm_id"]
    if baseline == candidate or baseline not in arms or candidate not in arms or len(arms) != 2:
        raise ContractValidationError(
            "comparison", "$.comparison", "comparison must bind the exact two metric arms"
        )
    tc_delta = arms[candidate]["tc_occ"]["value"] - arms[baseline]["tc_occ"]["value"]
    ta_delta = arms[candidate]["ta_occ"]["value_lower"] - arms[baseline]["ta_occ"]["value_lower"]
    if not math.isclose(comparison["tc_occ_delta"], tc_delta, rel_tol=0, abs_tol=1e-12):
        raise ContractValidationError("comparison", "$.comparison.tc_occ_delta", "delta mismatch")
    if not math.isclose(comparison["ta_occ_delta"], ta_delta, rel_tol=0, abs_tol=1e-12):
        raise ContractValidationError("comparison", "$.comparison.ta_occ_delta", "delta mismatch")


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"artifact_sha256"}, path=path)
    return {
        "artifact_sha256": require_sha256(
            row["artifact_sha256"], path=f"{path}.artifact_sha256"
        )
    }


def _rendered_surface(row: Mapping[str, Any]) -> str | None:
    for value in (
        row.get("target_surface"),
        (row.get("t3_code_score") or {}).get("target_quote_clean")
        if isinstance(row.get("t3_code_score"), Mapping)
        else None,
        row.get("target_quote"),
    ):
        if isinstance(value, str) and value.strip():
            return value
    return None


def _validate_target_span(row: Mapping[str, Any], *, target_text: str, path: str) -> None:
    start = row.get("target_start")
    end = row.get("target_end")
    if start is None and end is None:
        return
    start_int = require_int(start, path=f"{path}.target_start", minimum=0)
    end_int = require_int(end, path=f"{path}.target_end", minimum=0)
    if end_int <= start_int or end_int > len(target_text):
        raise ContractValidationError("target_span", path, "target span is outside target text")
    surface = row.get("target_surface")
    if not isinstance(surface, str) or target_text[start_int:end_int] != surface:
        raise ContractValidationError("target_span", path, "target surface does not match coordinates")


def _source_occurrence_id(
    *,
    block_id: str,
    term_id: str,
    source_start: int,
    source_end: int,
    source_surface: str,
) -> str:
    return "source-occ-" + _canonical_digest(
        {
            "block_id": block_id,
            "term_id": term_id,
            "source_start": source_start,
            "source_end": source_end,
            "source_surface": source_surface,
        }
    )[:24]


def _normalized_forms(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({_norm(value) for value in values if _norm(value)}))


def _string_tuple(value: Any, *, path: str) -> tuple[str, ...]:
    rows = tuple(
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    )
    if len(_normalized_forms(rows)) != len(rows):
        raise ContractValidationError(
            "duplicate", path, "accepted forms must be unique after normalization"
        )
    return rows


def _string_list(value: Any, *, path: str) -> list[str]:
    return [
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    ]


def _unique_strings(value: Any, *, path: str) -> list[str]:
    rows = _string_list(value, path=path)
    if len(rows) != len(set(rows)):
        raise ContractValidationError("duplicate", path, "values must be unique")
    return rows


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _ratio_equal(value: float, numerator: int, denominator: int) -> bool:
    return math.isclose(value, _ratio(numerator, denominator), rel_tol=0, abs_tol=1e-12)


def _norm(value: str) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFC", str(value or "")).casefold().strip()
    )


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_policy(payload: Mapping[str, Any]) -> CanonicalPolicy:
    set_paths: set[tuple[str, ...]] = {("ruler", "source_artifact_ids")}
    arms = payload.get("arms")
    if isinstance(arms, Mapping):
        for arm_id, arm_value in arms.items():
            if not isinstance(arm_id, str):
                continue
            set_paths.add(("arms", arm_id, "tc_occ", "violation_block_ids"))
            set_paths.add(("arms", arm_id, "ta_occ", "violation_block_ids"))
            if not isinstance(arm_value, Mapping):
                continue
            tc = arm_value.get("tc_occ")
            terms = tc.get("terms") if isinstance(tc, Mapping) else None
            if isinstance(terms, Mapping):
                for term_id in terms:
                    if isinstance(term_id, str):
                        set_paths.add(
                            (
                                "arms",
                                arm_id,
                                "tc_occ",
                                "terms",
                                term_id,
                                "dominant_renderings",
                            )
                        )
    return CanonicalPolicy(
        set_like_paths=frozenset(set_paths),
        semantic_sequence_paths=frozenset(
            {
                ("identity", "selected_chapter_ids"),
                ("caveats",),
            }
        ),
    )


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _contained_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(
            "path_containment", "$.output_root", "artifact path escapes output root"
        ) from exc
    return candidate


def _publish_bytes_create_only(path: Path, encoded: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError(
                "artifact_collision", str(path), "existing artifact bytes differ"
            )
        return False
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise ContractValidationError(
                    "artifact_collision", str(path), "concurrent artifact bytes differ"
                )
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)
