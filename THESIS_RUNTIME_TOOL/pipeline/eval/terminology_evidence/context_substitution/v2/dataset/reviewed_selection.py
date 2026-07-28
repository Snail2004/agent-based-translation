from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import ContractValidationError


ANNOTATION_SCHEMA_ID = "D2LPilotHumanAnnotationsV1"
ANNOTATION_SCHEMA_VERSION = "1.0.0"
ANNOTATION_POLICY_ID = "d2l_cst_three_reviewer_human_review_v1_4"
SENSE_RECORD_SCHEMA_ID = "D2LReviewedSenseContractRecordV1"
SENSE_RECORD_SCHEMA_VERSION = "1.0.0"

_TABLES = {
    "context": (
        "stage_b/pilot_context_review.csv",
        ("same_sense_label", "context_type", "context_validity"),
        frozenset({"context_type"}),
    ),
    "contrastive": (
        "stage_b/pilot_contrastive_review.csv",
        ("contrastive_label", "use_in_sense_boundary_test"),
        frozenset(),
    ),
    "candidate": (
        "stage_b/pilot_candidate_annotation.csv",
        (
            "applicability",
            "semantic_fit_label",
            "candidate_rank",
            "candidate_decision",
            "candidate_relation",
            "relation_to_candidate_instance_id",
        ),
        frozenset({"candidate_rank", "relation_to_candidate_instance_id"}),
    ),
}

_CST_TYPE_MAP = {
    "C1": "definition",
    "C2": "typical_usage",
    "C3": "domain_collocation",
    "C4": "syntactic_variation",
    "C5": "same_sense_difficult",
}


def load_frozen_review_selection(
    review_root: Path,
    *,
    source_pilot_manifest_sha256: str,
    pilot_terms: Sequence[Mapping[str, Any]] | None = None,
    pilot_contexts: Sequence[Mapping[str, Any]] | None = None,
    pilot_candidates: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Load only a completed immutable three-reviewer artifact."""

    review_root = review_root.resolve()
    if not review_root.is_dir():
        _fail("review_artifact", "$.review_artifact", "expected an immutable directory")
    manifest_path = review_root / "annotation_manifest.json"
    if not manifest_path.is_file():
        workflow_path = review_root / "manifest.json"
        if workflow_path.is_file():
            workflow = _load_json(workflow_path)
            status = str(workflow.get("status") or "UNKNOWN")
            if status != "COMPLETE_IMMUTABLE":
                _fail(
                    "review_status",
                    "$.review_artifact.status",
                    f"human review is not complete: {status}",
                )
        _fail(
            "review_artifact",
            "$.review_artifact",
            "completed annotation_manifest.json is missing",
        )
    manifest = _load_json(manifest_path)
    _require_equal(manifest, "schema_id", ANNOTATION_SCHEMA_ID, "$.annotation_manifest")
    _require_equal(
        manifest, "schema_version", ANNOTATION_SCHEMA_VERSION, "$.annotation_manifest"
    )
    _require_equal(manifest, "policy_id", ANNOTATION_POLICY_ID, "$.annotation_manifest")
    _require_equal(manifest, "status", "COMPLETE_IMMUTABLE", "$.annotation_manifest")
    _verify_object_self_hash(
        manifest, "annotation_manifest_sha256", path="$.annotation_manifest"
    )
    if manifest.get("source_pilot", {}).get("manifest_sha256") != source_pilot_manifest_sha256:
        _fail(
            "review_binding",
            "$.annotation_manifest.source_pilot",
            "review artifact is bound to another pilot",
        )
    _verify_files(review_root, manifest)

    sense_rows = _load_jsonl(review_root / "stage_a/reviewed_sense_contract.jsonl")
    senses: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(sense_rows):
        path = f"$.reviewed_sense_contract[{index}]"
        _require_equal(row, "schema_id", SENSE_RECORD_SCHEMA_ID, path)
        _require_equal(row, "schema_version", SENSE_RECORD_SCHEMA_VERSION, path)
        _require_equal(row, "policy_id", ANNOTATION_POLICY_ID, path)
        _verify_object_self_hash(row, "reviewed_sense_contract_sha256", path=path)
        sense_id = _require_string(row.get("sense_id"), f"{path}.sense_id")
        if sense_id in senses:
            _fail("duplicate", f"{path}.sense_id", "duplicate reviewed sense")
        _require_string(row.get("effective_definition_en"), f"{path}.effective_definition_en")
        _require_string(
            row.get("effective_part_of_speech"), f"{path}.effective_part_of_speech"
        )
        senses[sense_id] = dict(row)

    expected_terms = {
        str(row["sense_id"]): row for row in (pilot_terms or ())
    }
    if expected_terms:
        if set(senses) != set(expected_terms):
            _fail("review_cover", "$.reviewed_sense_contract", "sense coverage mismatch")
        for sense_id, reviewed in senses.items():
            source = expected_terms[sense_id]
            for field in ("term_id", "sense_id", "source_term", "scope_id"):
                if str(reviewed.get(field)) != str(source.get(field)):
                    _fail(
                        "review_binding",
                        f"$.reviewed_sense_contract[{sense_id}].{field}",
                        "reviewed sense differs from pilot",
                    )
            if reviewed.get("source_term_sense_sha256") != source.get("term_sense_sha256"):
                _fail(
                    "review_binding",
                    f"$.reviewed_sense_contract[{sense_id}].source_term_sense_sha256",
                    "reviewed sense source hash mismatch",
                )

    rows_by_table = {
        table: _read_csv(review_root / spec[0])
        for table, spec in _TABLES.items()
    }
    declared_counts = manifest.get("row_counts")
    if not isinstance(declared_counts, Mapping):
        _fail("review_counts", "$.annotation_manifest.row_counts", "expected object")
    for table, rows in rows_by_table.items():
        if int(declared_counts.get(table, -1)) != len(rows):
            _fail("review_counts", f"$.{table}", "row count mismatch")

    context_source = {
        str(row["context_id"]): row for row in (pilot_contexts or ())
    }
    candidate_source = {
        str(row["candidate_instance_id"]): row for row in (pilot_candidates or ())
    }
    selections: dict[str, dict[str, str]] = {}
    for table in ("context", "contrastive"):
        _, signature, optional = _TABLES[table]
        for index, row in enumerate(rows_by_table[table]):
            path = f"$.{table}[{index}]"
            decision = _resolve_decision(
                row,
                signature_fields=signature,
                optional_fields=optional,
                path=path,
            )
            context_id = _require_string(row.get("context_id"), f"{path}.context_id")
            if context_id in selections:
                _fail("duplicate", f"{path}.context_id", "context appears in two review tables")
            source = context_source.get(context_id)
            if context_source and source is None:
                _fail("review_binding", f"{path}.context_id", "foreign context")
            if source is not None:
                if row.get("source_record_sha256") != source.get("context_sha256"):
                    _fail("review_binding", f"{path}.source_record_sha256", "context hash mismatch")
                _validate_common_source_fields(row, source, path=path)
                expected_role = "CONTRASTIVE" if table == "contrastive" else source.get("context_role")
                if str(source.get("context_role")) != str(expected_role):
                    _fail("review_binding", f"{path}.context_id", "review table role mismatch")
            selections[context_id] = (
                _contrastive_selection(row, decision)
                if table == "contrastive"
                else _same_sense_selection(row, decision)
            )

    _, candidate_signature, candidate_optional = _TABLES["candidate"]
    reviewed_candidate_ids: set[str] = set()
    for index, row in enumerate(rows_by_table["candidate"]):
        path = f"$.candidate[{index}]"
        _resolve_decision(
            row,
            signature_fields=candidate_signature,
            optional_fields=candidate_optional,
            path=path,
        )
        candidate_id = _require_string(
            row.get("candidate_instance_id"), f"{path}.candidate_instance_id"
        )
        if candidate_id in reviewed_candidate_ids:
            _fail("duplicate", f"{path}.candidate_instance_id", "duplicate candidate review")
        reviewed_candidate_ids.add(candidate_id)
        source = candidate_source.get(candidate_id)
        if candidate_source and source is None:
            _fail("review_binding", f"{path}.candidate_instance_id", "foreign candidate")
        if source is not None:
            if row.get("source_record_sha256") != source.get("candidate_instance_sha256"):
                _fail("review_binding", f"{path}.source_record_sha256", "candidate hash mismatch")
            for field in (
                "term_id",
                "sense_id",
                "scope_id",
                "candidate_slot_id",
                "candidate_instance_id",
                "candidate_target_vi",
                "formation_method",
            ):
                if str(row.get(field)) != str(source.get(field)):
                    _fail("review_binding", f"{path}.{field}", "candidate source differs")

    if context_source and set(selections) != set(context_source):
        _fail("review_cover", "$.stage_b.contexts", "review does not cover all pilot contexts")
    if candidate_source and reviewed_candidate_ids != set(candidate_source):
        _fail("review_cover", "$.stage_b.candidates", "review does not cover all pilot candidates")

    sense_manifest_hash = _require_sha256(
        manifest.get("source_sense_contract", {}).get("manifest_sha256"),
        "$.annotation_manifest.source_sense_contract.manifest_sha256",
    )
    review_hash = _require_sha256(
        manifest.get("annotation_manifest_sha256"),
        "$.annotation_manifest.annotation_manifest_sha256",
    )
    return {
        "review_artifact_ref": manifest_path.as_posix(),
        "review_artifact_sha256": review_hash,
        "effective_sense_contract_ref": (
            review_root / "stage_a/reviewed_sense_contract.jsonl"
        ).as_posix(),
        "effective_sense_contract_sha256": sense_manifest_hash,
        "sense_inventory_version": f"reviewed-sense-contract:{sense_manifest_hash}",
        "senses": senses,
        "contexts": selections,
        "candidate_review_count": len(reviewed_candidate_ids),
    }


def _same_sense_selection(
    row: Mapping[str, str], decision: Mapping[str, str]
) -> dict[str, str]:
    same_sense = decision["same_sense_label"]
    validity = decision["context_validity"]
    context_type = decision.get("context_type") or "NOT_APPLICABLE"
    if same_sense == "SAME_SENSE" and validity in {"VALID", "WEAK"}:
        mapped_type = _CST_TYPE_MAP.get(context_type)
        if mapped_type is None:
            _fail("review_label", "$.context.context_type", "same-sense review requires C1-C5")
        relation = "SAME_SENSE"
        judgeability = "JUDGEABLE"
    elif validity == "INVALID":
        relation = "AMBIGUOUS"
        mapped_type = "unknown"
        judgeability = "INVALID_SOURCE"
    else:
        relation = "AMBIGUOUS"
        mapped_type = "unknown"
        judgeability = "AMBIGUOUS_SENSE"
    return {
        "sense_relation": relation,
        "context_type": mapped_type,
        "judgeability": judgeability,
        "reason": f"frozen_human_review:{same_sense}:{validity}:{context_type}",
        "review_row_sha256": _object_hash(dict(row)),
    }


def _contrastive_selection(
    row: Mapping[str, str], decision: Mapping[str, str]
) -> dict[str, str]:
    label = decision["contrastive_label"]
    use = decision["use_in_sense_boundary_test"]
    if label in {"VALID_BOUNDARY", "WEAK_BOUNDARY"} and use == "TRUE":
        relation = "CONTRASTIVE"
        context_type = "contrastive"
        judgeability = "JUDGEABLE"
    else:
        relation = "AMBIGUOUS"
        context_type = "unknown"
        judgeability = "INVALID_SOURCE"
    return {
        "sense_relation": relation,
        "context_type": context_type,
        "judgeability": judgeability,
        "reason": f"frozen_human_review:{label}:use={use}",
        "review_row_sha256": _object_hash(dict(row)),
    }


def _resolve_decision(
    row: Mapping[str, str],
    *,
    signature_fields: Sequence[str],
    optional_fields: frozenset[str],
    path: str,
) -> dict[str, str]:
    reviewer_ids: list[str] = []
    signatures: list[dict[str, str]] = []
    reviewer_times: list[datetime] = []
    for number in (1, 2, 3):
        prefix = f"reviewer_{number}"
        if row.get(f"{prefix}_status") != "REVIEWED":
            _fail("review_incomplete", path, f"{prefix} is not REVIEWED")
        reviewer_id = _require_string(row.get(f"{prefix}_id"), f"{path}.{prefix}_id")
        reviewer_ids.append(reviewer_id.casefold())
        reviewer_times.append(
            _iso8601(row.get(f"{prefix}_reviewed_at"), f"{path}.{prefix}_reviewed_at")
        )
        signature = {
            field: str(row.get(f"{prefix}_{field}", ""))
            for field in signature_fields
        }
        if any(not value for field, value in signature.items() if field not in optional_fields):
            _fail("review_incomplete", path, f"{prefix} decision signature is incomplete")
        signatures.append(signature)
    if len(set(reviewer_ids)) != 3:
        _fail("review_independence", path, "reviewer identities must be distinct")
    rendered = [_canonical_json(signature) for signature in signatures]
    winning, count = Counter(rendered).most_common(1)[0]
    if count >= 2:
        if _adjudication_populated(row):
            _fail("review_adjudication", path, "majority decision must not be adjudicated")
        return signatures[rendered.index(winning)]
    if row.get("adjudication_status") != "ADJUDICATED":
        _fail("review_incomplete", path, "three-way disagreement requires adjudication")
    adjudicator = _require_string(row.get("adjudicator_id"), f"{path}.adjudicator_id")
    if adjudicator.casefold() in set(reviewer_ids):
        _fail("review_independence", path, "adjudicator must be independent")
    adjudicated_at = _iso8601(row.get("adjudicated_at"), f"{path}.adjudicated_at")
    if any(adjudicated_at < value for value in reviewer_times):
        _fail("review_adjudication", path, "adjudication predates reviewer decision")
    decision = {
        field: str(row.get(f"adjudicated_{field}", ""))
        for field in signature_fields
    }
    if any(not value for field, value in decision.items() if field not in optional_fields):
        _fail("review_incomplete", path, "adjudicated signature is incomplete")
    return decision


def _validate_common_source_fields(
    row: Mapping[str, str], source: Mapping[str, Any], *, path: str
) -> None:
    for field in ("term_id", "sense_id", "context_id", "source_text", "content_sha256"):
        if str(row.get(field)) != str(source.get(field)):
            _fail("review_binding", f"{path}.{field}", "context source differs from pilot")


def _verify_files(root: Path, manifest: Mapping[str, Any]) -> None:
    bindings = manifest.get("files")
    if not isinstance(bindings, Mapping) or not bindings:
        _fail("review_files", "$.annotation_manifest.files", "expected bindings")
    seen: set[str] = set()
    for name, raw in bindings.items():
        if not isinstance(raw, Mapping):
            _fail("review_files", f"$.annotation_manifest.files.{name}", "expected object")
        ref = _safe_ref(raw.get("ref"), path=f"$.annotation_manifest.files.{name}.ref")
        if ref.casefold() in seen:
            _fail("review_files", "$.annotation_manifest.files", "case-confusable duplicate")
        seen.add(ref.casefold())
        if raw.get("mutable_after_annotation") is not False:
            _fail("review_files", f"$.annotation_manifest.files.{name}", "final artifact contains mutable file")
        expected = _require_sha256(raw.get("sha256"), f"$.annotation_manifest.files.{name}.sha256")
        path = root.joinpath(*PurePosixPath(ref).parts)
        if not path.is_file() or _sha256_file(path) != expected:
            _fail("review_files", f"$.annotation_manifest.files.{name}", "file hash mismatch")
    for required in (
        "stage_a/reviewed_sense_contract.jsonl",
        "stage_b/pilot_context_review.csv",
        "stage_b/pilot_contrastive_review.csv",
        "stage_b/pilot_candidate_annotation.csv",
    ):
        if required not in bindings:
            _fail("review_files", "$.annotation_manifest.files", f"missing {required}")


def _verify_object_self_hash(row: Mapping[str, Any], field: str, *, path: str) -> None:
    actual = _require_sha256(row.get(field), f"{path}.{field}")
    identity = dict(row)
    identity.pop(field, None)
    if actual != _object_hash(identity):
        _fail("self_hash", f"{path}.{field}", "hash mismatch")


def _adjudication_populated(row: Mapping[str, str]) -> bool:
    return any(
        value
        for key, value in row.items()
        if key.startswith(("adjudicat", "adjudicator_"))
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail("json", str(path), f"invalid JSON: {exc.__class__.__name__}")
    if not isinstance(value, dict):
        _fail("json", str(path), "expected object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            _fail("jsonl", f"{path}:{number}", "expected object")
        rows.append(value)
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _safe_ref(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        _fail("path", path, "invalid relative POSIX path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        _fail("path", path, "invalid relative POSIX path")
    if parsed.as_posix() != value:
        _fail("path", path, "noncanonical path")
    return value


def _require_equal(row: Mapping[str, Any], field: str, expected: str, path: str) -> None:
    if row.get(field) != expected:
        _fail("schema", f"{path}.{field}", f"expected {expected}")


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail("string", path, "expected canonical nonempty string")
    return value


def _require_sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        _fail("sha256", path, "invalid SHA-256")
    return value


def _iso8601(value: Any, path: str) -> datetime:
    text = _require_string(value, path)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail("timestamp", path, "invalid ISO-8601 timestamp")
    if parsed.tzinfo is None:
        _fail("timestamp", path, "timestamp requires timezone")
    return parsed


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(code: str, path: str, message: str) -> None:
    raise ContractValidationError(code, path, message)


__all__ = [
    "ANNOTATION_POLICY_ID",
    "ANNOTATION_SCHEMA_ID",
    "ANNOTATION_SCHEMA_VERSION",
    "load_frozen_review_selection",
]
