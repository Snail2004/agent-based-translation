from __future__ import annotations

import csv
import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping


FREEZE_SCHEMA_ID = "D2LContextSupportSetFreezeV1"
FREEZE_SCHEMA_VERSION = "1.0.0"
FREEZE_POLICY_ID = "d2l_context_support_set_freeze_v1"
TERM_SENSES_FILE = "term_senses.jsonl"
CONTEXTS_FILE = "contexts.jsonl"
CANDIDATE_SLOTS_FILE = "candidate_slots.jsonl"
CANDIDATE_INSTANCES_FILE = "candidate_instances.jsonl"
CANDIDATE_QUEUE_FILE = "candidate_generation_queue.jsonl"
GAPS_FILE = "gaps.jsonl"
ANNOTATION_FILE = "annotation_template.csv"
STATISTICS_FILE = "statistics.json"
VALIDATION_FILE = "validation_report.json"
MANIFEST_FILE = "manifest.json"

DATA_FILES = (
    TERM_SENSES_FILE,
    CONTEXTS_FILE,
    CANDIDATE_SLOTS_FILE,
    CANDIDATE_INSTANCES_FILE,
    CANDIDATE_QUEUE_FILE,
    GAPS_FILE,
    ANNOTATION_FILE,
    STATISTICS_FILE,
)

ANNOTATION_COLUMNS = (
    "term_id",
    "sense_id",
    "scope_id",
    "candidate_slot_id",
    "candidate_instance_id",
    "candidate_target_vi",
    "candidate_status",
    "shared_context_set_id",
    "annotator_id",
    "annotation_status",
    "semantic_fit_label",
    "preferred_rank",
    "accept_reject",
    "notes",
)


class FreezeValidationError(ValueError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def stable_id(prefix: str, *parts: str, length: int = 24) -> str:
    digest = sha256_text("\0".join(parts))
    return f"{prefix}_{digest[:length]}"


def canonical_row_hash(row: Mapping[str, Any], *, hash_field: str) -> str:
    payload = {key: value for key, value in row.items() if key != hash_field}
    return sha256_bytes(canonical_json_bytes(payload))


def seal_row(row: Mapping[str, Any], *, hash_field: str) -> dict[str, Any]:
    result = dict(row)
    result[hash_field] = canonical_row_hash(result, hash_field=hash_field)
    return result


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_json_bytes(dict(row)))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FreezeValidationError(
                f"{path.name}:{line_number}: invalid JSON"
            ) from exc
        if not isinstance(row, dict):
            raise FreezeValidationError(
                f"{path.name}:{line_number}: expected an object"
            )
        rows.append(row)
    return rows


def write_annotation_template(
    path: Path, rows: Iterable[Mapping[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in ANNOTATION_COLUMNS})


def _require_string(value: Any, *, path: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise FreezeValidationError(f"{path}: expected string")
    if not allow_empty and not value.strip():
        raise FreezeValidationError(f"{path}: empty string")
    return value


def _require_sha256(value: Any, *, path: str) -> str:
    text = _require_string(value, path=path)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FreezeValidationError(f"{path}: invalid SHA-256")
    return text


def _require_unique(values: Iterable[str], *, path: str) -> None:
    rows = list(values)
    if len(rows) != len(set(rows)):
        raise FreezeValidationError(f"{path}: duplicate values")


def _verify_row_hashes(
    rows: Iterable[Mapping[str, Any]], *, file_name: str, hash_field: str
) -> None:
    for index, row in enumerate(rows):
        expected = canonical_row_hash(row, hash_field=hash_field)
        actual = _require_sha256(
            row.get(hash_field), path=f"{file_name}[{index}].{hash_field}"
        )
        if actual != expected:
            raise FreezeValidationError(
                f"{file_name}[{index}].{hash_field}: hash mismatch"
            )


def validate_freeze_bundle(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise FreezeValidationError("manifest.json: expected object")
    if manifest.get("schema_id") != FREEZE_SCHEMA_ID:
        raise FreezeValidationError("manifest.json.schema_id: unsupported")
    if manifest.get("schema_version") != FREEZE_SCHEMA_VERSION:
        raise FreezeValidationError("manifest.json.schema_version: unsupported")
    if manifest.get("policy_id") != FREEZE_POLICY_ID:
        raise FreezeValidationError("manifest.json.policy_id: unsupported")

    expected_manifest_hash = canonical_row_hash(
        manifest, hash_field="manifest_sha256"
    )
    if manifest.get("manifest_sha256") != expected_manifest_hash:
        raise FreezeValidationError("manifest.json.manifest_sha256: hash mismatch")

    source_artifacts = manifest.get("source_artifacts")
    if not isinstance(source_artifacts, dict) or not source_artifacts:
        raise FreezeValidationError(
            "manifest.json.source_artifacts: expected nonempty object"
        )
    source_hash_by_ref: dict[str, str] = {}
    for name, raw_binding in source_artifacts.items():
        if not isinstance(raw_binding, dict):
            raise FreezeValidationError(
                f"manifest.json.source_artifacts.{name}: expected object"
            )
        ref = _require_string(
            raw_binding.get("ref"),
            path=f"manifest.json.source_artifacts.{name}.ref",
        )
        expected_sha = _require_sha256(
            raw_binding.get("physical_sha256"),
            path=(
                f"manifest.json.source_artifacts.{name}.physical_sha256"
            ),
        )
        source_path = Path(ref)
        if not source_path.is_file():
            raise FreezeValidationError(
                f"manifest.json.source_artifacts.{name}: source file missing"
            )
        if sha256_bytes(source_path.read_bytes()) != expected_sha:
            raise FreezeValidationError(
                f"manifest.json.source_artifacts.{name}: source hash mismatch"
            )
        source_hash_by_ref[ref] = expected_sha

    document_binding = source_artifacts.get("document")
    glossary_binding = source_artifacts.get("glossary")
    if not isinstance(document_binding, dict) or not isinstance(
        glossary_binding, dict
    ):
        raise FreezeValidationError(
            "manifest.json.source_artifacts: document/glossary bindings required"
        )
    document = json.loads(Path(document_binding["ref"]).read_text(encoding="utf-8"))
    glossary = json.loads(Path(glossary_binding["ref"]).read_text(encoding="utf-8"))
    document_blocks: dict[str, tuple[str, str]] = {}
    for chapter in document.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        chapter_id = chapter.get("chapter_id")
        for block in chapter.get("blocks", []):
            if not isinstance(block, dict):
                continue
            block_id = block.get("block_id")
            source_text = block.get("source_text")
            if (
                isinstance(chapter_id, str)
                and isinstance(block_id, str)
                and isinstance(source_text, str)
            ):
                document_blocks[block_id] = (chapter_id, source_text)
    glossary_record_ids = {
        str(row.get("record_id"))
        for row in glossary.get("records", [])
        if isinstance(row, dict) and row.get("record_id") is not None
    }

    file_bindings = manifest.get("files")
    if not isinstance(file_bindings, dict):
        raise FreezeValidationError("manifest.json.files: expected object")
    for name in DATA_FILES:
        binding = file_bindings.get(name)
        if not isinstance(binding, dict):
            raise FreezeValidationError(f"manifest.json.files.{name}: missing")
        path = output_dir / name
        if not path.is_file():
            raise FreezeValidationError(f"{name}: missing")
        expected_sha = _require_sha256(
            binding.get("sha256"), path=f"manifest.json.files.{name}.sha256"
        )
        actual_sha = sha256_bytes(path.read_bytes())
        if expected_sha != actual_sha:
            raise FreezeValidationError(f"{name}: physical hash mismatch")

    terms = read_jsonl(output_dir / TERM_SENSES_FILE)
    contexts = read_jsonl(output_dir / CONTEXTS_FILE)
    slots = read_jsonl(output_dir / CANDIDATE_SLOTS_FILE)
    candidates = read_jsonl(output_dir / CANDIDATE_INSTANCES_FILE)
    queue = read_jsonl(output_dir / CANDIDATE_QUEUE_FILE)
    gaps = read_jsonl(output_dir / GAPS_FILE)
    with (output_dir / ANNOTATION_FILE).open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        annotation_reader = csv.DictReader(handle)
        if tuple(annotation_reader.fieldnames or ()) != ANNOTATION_COLUMNS:
            raise FreezeValidationError(
                f"{ANNOTATION_FILE}: unexpected columns"
            )
        annotation_rows = list(annotation_reader)
    _verify_row_hashes(
        terms, file_name=TERM_SENSES_FILE, hash_field="term_sense_sha256"
    )
    _verify_row_hashes(
        contexts, file_name=CONTEXTS_FILE, hash_field="context_sha256"
    )
    _verify_row_hashes(
        slots, file_name=CANDIDATE_SLOTS_FILE, hash_field="candidate_slot_sha256"
    )
    _verify_row_hashes(
        candidates,
        file_name=CANDIDATE_INSTANCES_FILE,
        hash_field="candidate_instance_sha256",
    )
    _verify_row_hashes(
        queue,
        file_name=CANDIDATE_QUEUE_FILE,
        hash_field="generation_request_sha256",
    )
    _verify_row_hashes(gaps, file_name=GAPS_FILE, hash_field="gap_sha256")

    requested = manifest.get("requested_cardinality")
    if not isinstance(requested, dict):
        raise FreezeValidationError(
            "manifest.json.requested_cardinality: expected object"
        )
    term_count = int(requested.get("term_sense_count", -1))
    candidate_count = int(requested.get("candidates_per_sense", -1))
    primary_count = int(requested.get("primary_contexts_per_sense", -1))
    backup_count = int(requested.get("backup_contexts_per_sense", -1))
    if len(terms) != term_count:
        raise FreezeValidationError(
            f"{TERM_SENSES_FILE}: expected {term_count}, found {len(terms)}"
        )
    if len(slots) != term_count * candidate_count:
        raise FreezeValidationError(
            f"{CANDIDATE_SLOTS_FILE}: expected "
            f"{term_count * candidate_count}, found {len(slots)}"
        )
    if len(contexts) != term_count * (primary_count + backup_count):
        raise FreezeValidationError(
            f"{CONTEXTS_FILE}: expected "
            f"{term_count * (primary_count + backup_count)}, "
            f"found {len(contexts)}"
        )
    if len(annotation_rows) != len(slots):
        raise FreezeValidationError(
            f"{ANNOTATION_FILE}: expected {len(slots)} rows, "
            f"found {len(annotation_rows)}"
        )
    if len(gaps) != term_count:
        raise FreezeValidationError(
            f"{GAPS_FILE}: expected {term_count} rows, found {len(gaps)}"
        )

    term_keys = {(row.get("term_id"), row.get("sense_id")) for row in terms}
    _require_unique(
        (
            _require_string(row.get("term_id"), path=f"terms[{index}].term_id")
            + "\0"
            + _require_string(
                row.get("sense_id"), path=f"terms[{index}].sense_id"
            )
            for index, row in enumerate(terms)
        ),
        path="term_senses",
    )
    context_ids = [
        _require_string(row.get("context_id"), path=f"contexts[{index}].context_id")
        for index, row in enumerate(contexts)
    ]
    _require_unique(context_ids, path="contexts.context_id")
    slot_ids = [
        _require_string(
            row.get("candidate_slot_id"), path=f"slots[{index}].candidate_slot_id"
        )
        for index, row in enumerate(slots)
    ]
    _require_unique(slot_ids, path="candidate_slots.candidate_slot_id")
    candidate_ids = [
        _require_string(
            row.get("candidate_instance_id"),
            path=f"candidates[{index}].candidate_instance_id",
        )
        for index, row in enumerate(candidates)
    ]
    _require_unique(candidate_ids, path="candidate_instances.candidate_instance_id")

    contexts_by_term: dict[tuple[Any, Any], list[Mapping[str, Any]]] = {}
    for row in contexts:
        key = (row.get("term_id"), row.get("sense_id"))
        if key not in term_keys:
            raise FreezeValidationError("contexts: foreign term-sense binding")
        contexts_by_term.setdefault(key, []).append(row)
        text = _require_string(row.get("source_text"), path="contexts.source_text")
        if row.get("content_sha256") != sha256_text(text):
            raise FreezeValidationError("contexts.content_sha256: hash mismatch")
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            raise FreezeValidationError("contexts.provenance: expected object")
        start = provenance.get("source_start")
        end = provenance.get("source_end")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            raise FreezeValidationError("contexts.provenance: invalid source range")
        _require_string(provenance.get("chapter_id"), path="provenance.chapter_id")
        _require_string(provenance.get("block_id"), path="provenance.block_id")
        _require_string(provenance.get("sentence_id"), path="provenance.sentence_id")
        _require_sha256(
            provenance.get("source_artifact_sha256"),
            path="provenance.source_artifact_sha256",
        )
        source_ref = _require_string(
            provenance.get("source_artifact_ref"),
            path="provenance.source_artifact_ref",
        )
        if source_hash_by_ref.get(source_ref) != provenance.get(
            "source_artifact_sha256"
        ):
            raise FreezeValidationError(
                "contexts.provenance: foreign source artifact binding"
            )
        block_id = str(provenance["block_id"])
        source_block = document_blocks.get(block_id)
        if source_block is None:
            raise FreezeValidationError(
                "contexts.provenance: source block not found"
            )
        source_chapter_id, block_text = source_block
        if source_chapter_id != provenance["chapter_id"]:
            raise FreezeValidationError(
                "contexts.provenance: chapter/block mismatch"
            )
        if sha256_text(block_text) != provenance.get("block_text_sha256"):
            raise FreezeValidationError(
                "contexts.provenance: block hash mismatch"
            )
        if block_text[start:end] != text:
            raise FreezeValidationError(
                "contexts.provenance: source slice mismatch"
            )
        match_start = row.get("match_start")
        match_end = row.get("match_end")
        if (
            isinstance(match_start, bool)
            or not isinstance(match_start, int)
            or isinstance(match_end, bool)
            or not isinstance(match_end, int)
            or match_start < 0
            or match_end <= match_start
            or match_end > len(block_text)
        ):
            raise FreezeValidationError("contexts: invalid match range")
        matched = unicodedata.normalize(
            "NFC", block_text[match_start:match_end]
        ).casefold()
        if matched != row.get("matched_surface"):
            raise FreezeValidationError("contexts: matched surface mismatch")

    for key, rows in contexts_by_term.items():
        primary = [row for row in rows if row.get("context_role") == "PRIMARY"]
        backup = [row for row in rows if row.get("context_role") == "BACKUP"]
        if len(primary) != primary_count or len(backup) != backup_count:
            raise FreezeValidationError(
                f"contexts: invalid primary/backup count for {key}"
            )
        if len({row.get("content_sha256") for row in rows}) != len(rows):
            raise FreezeValidationError(f"contexts: duplicate text for {key}")

    context_ids_by_term = {
        key: {str(row["context_id"]) for row in rows}
        for key, rows in contexts_by_term.items()
    }
    for row in terms:
        key = (row.get("term_id"), row.get("sense_id"))
        declared_context_ids = {
            *row.get("primary_context_ids", []),
            *row.get("backup_context_ids", []),
        }
        if declared_context_ids != context_ids_by_term.get(key, set()):
            raise FreezeValidationError(
                f"term_senses: context binding mismatch for {key}"
            )
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            raise FreezeValidationError("term_senses.provenance: expected object")
        source_ref = _require_string(
            provenance.get("source_artifact_ref"),
            path="term_senses.provenance.source_artifact_ref",
        )
        if source_hash_by_ref.get(source_ref) != provenance.get(
            "source_artifact_sha256"
        ):
            raise FreezeValidationError(
                "term_senses.provenance: foreign source artifact binding"
            )
        if str(provenance.get("source_record_id")) not in glossary_record_ids:
            raise FreezeValidationError(
                "term_senses.provenance: source record not found"
            )

    slots_by_term: dict[tuple[Any, Any], list[Mapping[str, Any]]] = {}
    for row in slots:
        key = (row.get("term_id"), row.get("sense_id"))
        if key not in term_keys:
            raise FreezeValidationError("candidate_slots: foreign term-sense binding")
        slots_by_term.setdefault(key, []).append(row)
    for key, rows in slots_by_term.items():
        if len(rows) != candidate_count:
            raise FreezeValidationError(
                f"candidate_slots: expected {candidate_count} for {key}"
            )
        targets = [
            str(row["candidate_target_vi"]).strip().casefold()
            for row in rows
            if row.get("status") == "RECORDED"
        ]
        if len(targets) != len(set(targets)):
            raise FreezeValidationError(
                f"candidate_slots: duplicate recorded target for {key}"
            )

    candidate_slot_ids = {row["candidate_slot_id"] for row in slots}
    for row in candidates:
        if row.get("candidate_slot_id") not in candidate_slot_ids:
            raise FreezeValidationError(
                "candidate_instances: foreign candidate slot"
            )
        _require_string(
            row.get("candidate_target_vi"),
            path="candidate_instances.candidate_target_vi",
        )
        formation = row.get("formation_provenance")
        if not isinstance(formation, list) or not formation:
            raise FreezeValidationError(
                "candidate_instances.formation_provenance: missing"
            )
        for evidence in formation:
            if not isinstance(evidence, dict):
                raise FreezeValidationError(
                    "candidate_instances.formation_provenance: expected object"
                )
            source_ref = _require_string(
                evidence.get("source_artifact_ref"),
                path="candidate_instances.formation_provenance.source_artifact_ref",
            )
            if source_hash_by_ref.get(source_ref) != evidence.get(
                "source_artifact_sha256"
            ):
                raise FreezeValidationError(
                    "candidate_instances: foreign source artifact binding"
                )

    issue_counts = {
        "missing_candidate_slots": sum(
            row.get("status") != "RECORDED" for row in slots
        ),
        "missing_part_of_speech": sum(
            row.get("part_of_speech_status") != "RECORDED" for row in terms
        ),
        "pending_context_classification": sum(
            row.get("sense_relation") == "PENDING_CONTEXT_SELECTOR"
            or row.get("context_type") is None
            for row in contexts
        ),
        "missing_contrastive_contexts": sum(
            not row.get("contrastive_context_ids") for row in terms
        ),
    }
    gap_keys = [
        (row.get("term_id"), row.get("sense_id")) for row in gaps
    ]
    _require_unique(
        (f"{term_id}\0{sense_id}" for term_id, sense_id in gap_keys),
        path="gaps.term_sense",
    )
    if set(gap_keys) != term_keys:
        raise FreezeValidationError("gaps: term-sense coverage mismatch")
    if sum(
        len(row.get("missing_candidate_slot_ids", [])) for row in gaps
    ) != issue_counts["missing_candidate_slots"]:
        raise FreezeValidationError("gaps: missing candidate count mismatch")
    if sum(bool(row.get("missing_part_of_speech")) for row in gaps) != (
        issue_counts["missing_part_of_speech"]
    ):
        raise FreezeValidationError("gaps: missing POS count mismatch")
    if sum(
        int(row.get("pending_context_classification_count", -1))
        for row in gaps
    ) != issue_counts["pending_context_classification"]:
        raise FreezeValidationError(
            "gaps: pending context classification count mismatch"
        )
    if any(
        int(row.get("missing_primary_context_count", -1))
        or int(row.get("missing_backup_context_count", -1))
        for row in gaps
    ):
        raise FreezeValidationError("gaps: required contexts are missing")
    blocking_issue_count = (
        issue_counts["missing_candidate_slots"]
        + issue_counts["missing_part_of_speech"]
    )
    ready_for_context_selection = blocking_issue_count == 0
    ready_for_cst = not any(issue_counts.values())
    expected_status = (
        "READY_FOR_CONTEXT_SELECTION"
        if ready_for_context_selection
        else "BLOCKED_MISSING_REQUIRED_FIELDS"
    )
    if manifest.get("status") != expected_status:
        raise FreezeValidationError(
            "manifest.json.status: inconsistent with validation result"
        )
    return {
        "schema_id": "D2LContextSupportSetFreezeValidationV1",
        "schema_version": "1.0.0",
        "status": (
            "PASS"
            if ready_for_cst
            else "PASS_WITH_DECLARED_GAPS"
        ),
        "ready_for_context_selection": ready_for_context_selection,
        "ready_for_cst": ready_for_cst,
        "counts": {
            "term_senses": len(terms),
            "candidate_slots": len(slots),
            "candidate_instances": len(candidates),
            "contexts": len(contexts),
            "candidate_generation_requests": len(queue),
            "gap_records": len(gaps),
        },
        "issues": issue_counts,
        "blocking_issue_count": blocking_issue_count,
        "manifest_sha256": manifest["manifest_sha256"],
    }


