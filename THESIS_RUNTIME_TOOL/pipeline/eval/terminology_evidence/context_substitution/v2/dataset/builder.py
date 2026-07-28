from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.contract import (
    ANNOTATION_FILE,
    CANDIDATE_INSTANCES_FILE,
    CANDIDATE_QUEUE_FILE,
    CANDIDATE_SLOTS_FILE,
    CONTEXTS_FILE,
    DATA_FILES,
    FREEZE_POLICY_ID,
    FREEZE_SCHEMA_ID,
    FREEZE_SCHEMA_VERSION,
    GAPS_FILE,
    MANIFEST_FILE,
    STATISTICS_FILE,
    TERM_SENSES_FILE,
    VALIDATION_FILE,
    canonical_row_hash,
    seal_row,
    sha256_bytes,
    stable_id,
    validate_freeze_bundle,
    write_annotation_template,
    write_json,
    write_jsonl,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.sources import (
    artifact_binding,
    build_context_candidates,
    candidate_index_rows,
    classify_stratum,
    collect_candidate_evidence,
    flatten_document,
    glossary_records,
    load_json,
    normalize_target,
    select_diverse_contexts,
)


_STRATA = ("clear", "ambiguous", "collision_or_multi_target")


def _sample_records(
    rows: Sequence[Mapping[str, Any]],
    *,
    sample_size: int,
    seed: str,
    candidate_evidence: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    if sample_size < 1:
        raise ValueError("sample_size must be >= 1")
    if len(rows) < sample_size:
        raise ValueError(
            f"only {len(rows)} term-senses have enough lexical contexts; "
            f"{sample_size} requested"
        )
    populations: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        populations[classify_stratum(row["value"])].append(row)

    def ordering(row: Mapping[str, Any]) -> tuple[int, str, str]:
        term_id = str(row["record_id"])
        candidate_count = len(candidate_evidence.get(term_id, ()))
        return (
            0 if candidate_count >= 3 else 1,
            stable_id("sample", seed, term_id, length=64),
            term_id,
        )

    ordered = {
        name: sorted(populations.get(name, []), key=ordering) for name in _STRATA
    }
    base, remainder = divmod(sample_size, len(_STRATA))
    quotas = {
        name: min(len(ordered[name]), base + (index < remainder))
        for index, name in enumerate(_STRATA)
    }
    remaining = sample_size - sum(quotas.values())
    while remaining:
        progressed = False
        for name in _STRATA:
            if quotas[name] < len(ordered[name]):
                quotas[name] += 1
                remaining -= 1
                progressed = True
                if not remaining:
                    break
        if not progressed:
            raise AssertionError("sample redistribution exhausted")
    return [
        row
        for name in _STRATA
        for row in ordered[name][: int(quotas[name])]
    ]


def _part_of_speech(value: Mapping[str, Any]) -> tuple[str | None, str]:
    for key in ("part_of_speech", "pos", "word_class"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip(), "RECORDED"
    return None, "MISSING_IN_SOURCE_ARTIFACTS"


def _term_row(
    *,
    record: Mapping[str, Any],
    contexts: Sequence[Mapping[str, Any]],
    glossary_binding: Mapping[str, str],
    glossary_index: int,
    dataset_version: str,
    created_at: str,
) -> dict[str, Any]:
    value = record["value"]
    term_id = str(record["record_id"])
    sense_id = str(value.get("entry_id") or term_id)
    scope_id = str(value.get("chapter_id") or "d2l_book")
    part_of_speech, pos_status = _part_of_speech(value)
    shared_context_set_id = stable_id(
        "context_set", dataset_version, term_id, sense_id
    )
    return seal_row(
        {
            "schema_id": "D2LContextSupportTermSenseV1",
            "schema_version": "1.0.0",
            "dataset_version": dataset_version,
            "created_at": created_at,
            "term_id": term_id,
            "sense_id": sense_id,
            "scope_id": scope_id,
            "source_term": str(value.get("canonical_source") or ""),
            "definition": str(value.get("decision_rationale") or ""),
            "definition_status": "UNVERIFIED_GLOSSARY_DECISION_RATIONALE",
            "part_of_speech": part_of_speech,
            "part_of_speech_status": pos_status,
            "surfaces": list(value.get("surfaces") or []),
            "stratum": classify_stratum(value),
            "shared_context_set_id": shared_context_set_id,
            "primary_context_ids": [
                row["context_id"]
                for row in contexts
                if row["context_role"] == "PRIMARY"
            ],
            "backup_context_ids": [
                row["context_id"]
                for row in contexts
                if row["context_role"] == "BACKUP"
            ],
            "contrastive_context_ids": [],
            "contrastive_context_status": "NOT_CLASSIFIED",
            "source_member_candidate_ids": list(
                value.get("source_member_candidate_ids") or []
            ),
            "provenance": {
                "source_artifact_ref": glossary_binding["ref"],
                "source_artifact_sha256": glossary_binding["physical_sha256"],
                "source_json_path": f"$.records[{glossary_index}]",
                "source_record_id": term_id,
            },
        },
        hash_field="term_sense_sha256",
    )


def _context_rows(
    *,
    term_id: str,
    sense_id: str,
    shared_context_set_id: str,
    selected: Sequence[Mapping[str, Any]],
    primary_context_count: int,
    dataset_version: str,
    created_at: str,
) -> list[dict[str, Any]]:
    rows = []
    for index, context in enumerate(selected):
        primary = index < primary_context_count
        slot_index = index + 1 if primary else index - primary_context_count + 1
        rows.append(
            seal_row(
                {
                    "schema_id": "D2LContextSupportContextV1",
                    "schema_version": "1.0.0",
                    "dataset_version": dataset_version,
                    "created_at": created_at,
                    "term_id": term_id,
                    "sense_id": sense_id,
                    "shared_context_set_id": shared_context_set_id,
                    "context_id": context["context_id"],
                    "context_role": "PRIMARY" if primary else "BACKUP",
                    "context_slot": (
                        f"C{slot_index}" if primary else f"B{slot_index}"
                    ),
                    "candidate_neutral": True,
                    "binding_kind": "EXACT_SURFACE_MATCH_CANDIDATE_NEUTRAL",
                    "sense_relation": "PENDING_CONTEXT_SELECTOR",
                    "context_type": None,
                    "source_text": context["source_text"],
                    "content_sha256": context["content_sha256"],
                    "matched_surface": context["matched_surface"],
                    "match_start": context["match_start"],
                    "match_end": context["match_end"],
                    "provenance": context["provenance"],
                },
                hash_field="context_sha256",
            )
        )
    return rows


def _candidate_rows(
    *,
    term: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    candidates_per_sense: int,
    dataset_version: str,
    created_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    slots: list[dict[str, Any]] = []
    instances: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    for index in range(candidates_per_sense):
        slot_number = index + 1
        slot_id = stable_id(
            "candidate_slot",
            dataset_version,
            str(term["term_id"]),
            str(term["sense_id"]),
            str(slot_number),
        )
        recorded = evidence[index] if index < len(evidence) else None
        instance_id = (
            stable_id(
                "candidate",
                dataset_version,
                str(term["term_id"]),
                str(term["sense_id"]),
                normalize_target(str(recorded["target_vi"])),
            )
            if recorded is not None
            else None
        )
        slot = seal_row(
            {
                "schema_id": "D2LContextSupportCandidateSlotV1",
                "schema_version": "1.0.0",
                "dataset_version": dataset_version,
                "created_at": created_at,
                "term_id": term["term_id"],
                "sense_id": term["sense_id"],
                "scope_id": term["scope_id"],
                "shared_context_set_id": term["shared_context_set_id"],
                "slot_number": slot_number,
                "candidate_slot_id": slot_id,
                "candidate_instance_id": instance_id,
                "candidate_target_vi": (
                    recorded["target_vi"] if recorded is not None else None
                ),
                "status": (
                    "RECORDED"
                    if recorded is not None
                    else "MISSING_GENERATION"
                ),
                "formation_method": (
                    "RECORDED_PIPELINE_OUTPUT"
                    if recorded is not None
                    else "REQUIRES_CANDIDATE_GENERATOR"
                ),
            },
            hash_field="candidate_slot_sha256",
        )
        slots.append(slot)
        if recorded is not None:
            instances.append(
                seal_row(
                    {
                        "schema_id": "D2LContextSupportCandidateInstanceV1",
                        "schema_version": "1.0.0",
                        "dataset_version": dataset_version,
                        "created_at": created_at,
                        "term_id": term["term_id"],
                        "sense_id": term["sense_id"],
                        "scope_id": term["scope_id"],
                        "shared_context_set_id": term[
                            "shared_context_set_id"
                        ],
                        "candidate_slot_id": slot_id,
                        "candidate_instance_id": instance_id,
                        "candidate_target_vi": recorded["target_vi"],
                        "applicability": recorded["applicability"],
                        "formation_method": "RECORDED_PIPELINE_OUTPUT",
                        "formation_provenance": recorded["evidence"],
                    },
                    hash_field="candidate_instance_sha256",
                )
            )
        else:
            queue.append(
                seal_row(
                    {
                        "schema_id": "D2LContextCandidateGenerationRequestV1",
                        "schema_version": "1.0.0",
                        "dataset_version": dataset_version,
                        "created_at": created_at,
                        "term_id": term["term_id"],
                        "sense_id": term["sense_id"],
                        "scope_id": term["scope_id"],
                        "source_term": term["source_term"],
                        "definition": term["definition"],
                        "part_of_speech": term["part_of_speech"],
                        "candidate_slot_id": slot_id,
                        "slot_number": slot_number,
                        "shared_context_set_id": term[
                            "shared_context_set_id"
                        ],
                        "recorded_candidate_targets": [
                            row["target_vi"] for row in evidence
                        ],
                        "instruction": (
                            "Generate one distinct Vietnamese candidate for "
                            "this exact term-sense; do not duplicate any "
                            "recorded target."
                        ),
                    },
                    hash_field="generation_request_sha256",
                )
            )
    return slots, instances, queue


def build_support_set_freeze(
    *,
    candidate_index_path: Path,
    glossary_path: Path,
    document_path: Path,
    candidate_artifact_paths: Sequence[Path],
    output_dir: Path,
    sample_size: int = 150,
    candidates_per_sense: int = 3,
    primary_context_count: int = 5,
    backup_context_count: int = 3,
    seed: str = "d2l_context_support_freeze_v1",
    dataset_version: str = "d2l_context_support_freeze_v1",
    created_at: str,
) -> dict[str, Any]:
    candidate_index = load_json(candidate_index_path)
    glossary = load_json(glossary_path)
    document = load_json(document_path)
    if not isinstance(candidate_index, dict):
        raise ValueError("candidate index must be an object")
    if not isinstance(glossary, dict):
        raise ValueError("glossary must be an object")
    if not isinstance(document, dict):
        raise ValueError("document must be an object")
    candidate_index_rows(candidate_index)
    records = glossary_records(glossary)
    document_binding = artifact_binding(document_path)
    _, document_blocks = flatten_document(
        document, document_path=document_path
    )
    context_candidates, _ = build_context_candidates(
        records=records,
        document_blocks=document_blocks,
        document_binding=document_binding,
    )
    required_context_count = primary_context_count + backup_context_count
    eligible = [
        record
        for record in records
        if len(context_candidates.get(str(record["record_id"]), []))
        >= required_context_count
    ]
    candidate_evidence = collect_candidate_evidence(
        glossary=glossary,
        glossary_path=glossary_path,
        candidate_artifact_paths=candidate_artifact_paths,
    )
    selected = _sample_records(
        eligible,
        sample_size=sample_size,
        seed=seed,
        candidate_evidence=candidate_evidence,
    )
    glossary_binding = artifact_binding(glossary_path)
    glossary_indices = {
        str(record["record_id"]): index for index, record in enumerate(records)
    }

    terms: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    slots: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    annotation_rows: list[dict[str, Any]] = []
    for record in selected:
        term_id = str(record["record_id"])
        value = record["value"]
        sense_id = str(value.get("entry_id") or term_id)
        selected_contexts = select_diverse_contexts(
            context_candidates[term_id], count=required_context_count
        )
        shared_context_set_id = stable_id(
            "context_set", dataset_version, term_id, sense_id
        )
        context_rows = _context_rows(
            term_id=term_id,
            sense_id=sense_id,
            shared_context_set_id=shared_context_set_id,
            selected=selected_contexts,
            primary_context_count=primary_context_count,
            dataset_version=dataset_version,
            created_at=created_at,
        )
        term = _term_row(
            record=record,
            contexts=context_rows,
            glossary_binding=glossary_binding,
            glossary_index=glossary_indices[term_id],
            dataset_version=dataset_version,
            created_at=created_at,
        )
        term_slots, term_candidates, term_queue = _candidate_rows(
            term=term,
            evidence=candidate_evidence.get(term_id, ()),
            candidates_per_sense=candidates_per_sense,
            dataset_version=dataset_version,
            created_at=created_at,
        )
        terms.append(term)
        contexts.extend(context_rows)
        slots.extend(term_slots)
        candidates.extend(term_candidates)
        queue.extend(term_queue)
        gaps.append(
            seal_row(
                {
                    "schema_id": "D2LContextSupportSetGapV1",
                    "schema_version": "1.0.0",
                    "dataset_version": dataset_version,
                    "created_at": created_at,
                    "term_id": term_id,
                    "sense_id": sense_id,
                    "missing_candidate_slot_ids": [
                        row["candidate_slot_id"]
                        for row in term_slots
                        if row["status"] != "RECORDED"
                    ],
                    "missing_part_of_speech": (
                        term["part_of_speech_status"] != "RECORDED"
                    ),
                    "missing_primary_context_count": max(
                        0,
                        primary_context_count
                        - sum(
                            row["context_role"] == "PRIMARY"
                            for row in context_rows
                        ),
                    ),
                    "missing_backup_context_count": max(
                        0,
                        backup_context_count
                        - sum(
                            row["context_role"] == "BACKUP"
                            for row in context_rows
                        ),
                    ),
                    "pending_context_classification_count": sum(
                        row["sense_relation"] == "PENDING_CONTEXT_SELECTOR"
                        or row["context_type"] is None
                        for row in context_rows
                    ),
                    "contrastive_context_status": term[
                        "contrastive_context_status"
                    ],
                },
                hash_field="gap_sha256",
            )
        )
        for slot in term_slots:
            annotation_rows.append(
                {
                    "term_id": term_id,
                    "sense_id": sense_id,
                    "scope_id": term["scope_id"],
                    "candidate_slot_id": slot["candidate_slot_id"],
                    "candidate_instance_id": slot["candidate_instance_id"] or "",
                    "candidate_target_vi": slot["candidate_target_vi"] or "",
                    "candidate_status": slot["status"],
                    "shared_context_set_id": shared_context_set_id,
                    "annotator_id": "",
                    "annotation_status": "",
                    "semantic_fit_label": "",
                    "preferred_rank": "",
                    "accept_reject": "",
                    "notes": "",
                }
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / TERM_SENSES_FILE, terms)
    write_jsonl(output_dir / CONTEXTS_FILE, contexts)
    write_jsonl(output_dir / CANDIDATE_SLOTS_FILE, slots)
    write_jsonl(output_dir / CANDIDATE_INSTANCES_FILE, candidates)
    write_jsonl(output_dir / CANDIDATE_QUEUE_FILE, queue)
    write_jsonl(output_dir / GAPS_FILE, gaps)
    write_annotation_template(output_dir / ANNOTATION_FILE, annotation_rows)

    stratum_counts = Counter(row["stratum"] for row in terms)
    chapter_counts = Counter(
        row["provenance"]["chapter_id"] for row in contexts
    )
    statistics = {
        "schema_id": "D2LContextSupportSetStatisticsV1",
        "schema_version": "1.0.0",
        "dataset_version": dataset_version,
        "term_sense_count": len(terms),
        "candidate_slot_count": len(slots),
        "candidate_instance_count": len(candidates),
        "missing_candidate_slot_count": len(queue),
        "context_count": len(contexts),
        "primary_context_count": sum(
            row["context_role"] == "PRIMARY" for row in contexts
        ),
        "backup_context_count": sum(
            row["context_role"] == "BACKUP" for row in contexts
        ),
        "contrastive_context_count": 0,
        "part_of_speech_recorded_count": sum(
            row["part_of_speech_status"] == "RECORDED" for row in terms
        ),
        "stratum_counts": dict(sorted(stratum_counts.items())),
        "context_chapter_counts": dict(sorted(chapter_counts.items())),
        "eligible_term_sense_population": len(eligible),
        "source_glossary_population": len(records),
    }
    write_json(output_dir / STATISTICS_FILE, statistics)

    source_paths = {
        "candidate_index": candidate_index_path,
        "glossary": glossary_path,
        "document": document_path,
        **{
            f"candidate_artifact_{index + 1}": path
            for index, path in enumerate(candidate_artifact_paths)
        },
    }
    missing_pos = sum(
        row["part_of_speech_status"] != "RECORDED" for row in terms
    )
    status = (
        "READY_FOR_CONTEXT_SELECTION"
        if not queue and not missing_pos
        else "BLOCKED_MISSING_REQUIRED_FIELDS"
    )
    manifest = {
        "schema_id": FREEZE_SCHEMA_ID,
        "schema_version": FREEZE_SCHEMA_VERSION,
        "policy_id": FREEZE_POLICY_ID,
        "dataset_version": dataset_version,
        "created_at": created_at,
        "status": status,
        "seed": seed,
        "requested_cardinality": {
            "term_sense_count": sample_size,
            "candidates_per_sense": candidates_per_sense,
            "primary_contexts_per_sense": primary_context_count,
            "backup_contexts_per_sense": backup_context_count,
            "contrastive_contexts_per_sense": "0_PENDING_CLASSIFICATION",
        },
        "source_artifacts": {
            name: artifact_binding(path) for name, path in source_paths.items()
        },
        "files": {
            name: {
                "ref": name,
                "sha256": sha256_bytes((output_dir / name).read_bytes()),
            }
            for name in DATA_FILES
        },
        "declared_gaps": {
            "missing_candidate_slots": len(queue),
            "missing_part_of_speech": missing_pos,
            "pending_context_classification": len(contexts),
            "missing_contrastive_contexts": len(terms),
        },
    }
    manifest["manifest_sha256"] = canonical_row_hash(
        manifest, hash_field="manifest_sha256"
    )
    write_json(output_dir / MANIFEST_FILE, manifest)
    validation = validate_freeze_bundle(output_dir)
    write_json(output_dir / VALIDATION_FILE, validation)
    return {
        "manifest": manifest,
        "validation": validation,
        "statistics": statistics,
    }


