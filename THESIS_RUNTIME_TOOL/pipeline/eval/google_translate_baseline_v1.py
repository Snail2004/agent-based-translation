from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import socket
import sqlite3
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.d2l_input_v1 import validate_d2l_evaluation_input


__all__ = [
    "GoogleTranslateBaselineError",
    "GoogleTranslateConfigV1",
    "GoogleTranslateTransportError",
    "build_google_translate_source_input_v1",
    "build_google_translate_plan_v1",
    "execute_google_translate_plan_v1",
    "parse_google_translated_html_v1",
    "validate_google_translate_capture_v1",
    "validate_google_translate_checkpoint_v1",
    "validate_google_translate_plan_v1",
    "validate_google_translate_source_input_v1",
]


PLAN_SCHEMA_ID = "GoogleTranslateBaselinePlanV1"
CHECKPOINT_SCHEMA_ID = "GoogleTranslateBaselineCheckpointV1"
CAPTURE_SCHEMA_ID = "GoogleTranslateBaselineCaptureV1"
SOURCE_INPUT_SCHEMA_ID = "GoogleTranslateSourceInputV1"
SCHEMA_VERSION = "1.0.0"
GOOGLE_BASIC_V2_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"
GOOGLE_NMT_MODEL = "nmt"
DEFAULT_CHAPTER_ID = "d2l_multilayer_perceptrons"
SOURCE_ADMISSION_POLICY = {
    "policy_id": "evaluation.google_translate.d2l_source_admission.v1",
    "policy_version": "1.0.0",
    "translate_block_types": ["heading", "prose"],
    "all_other_block_types": "preserve",
}


class GoogleTranslateBaselineError(RuntimeError):
    pass


class GoogleTranslateTransportError(GoogleTranslateBaselineError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        outcome_known: bool,
        http_status: int | None = None,
    ) -> None:
        self.error_code = error_code
        self.outcome_known = outcome_known
        self.http_status = http_status
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GoogleTranslateConfigV1:
    chapter_id: str = DEFAULT_CHAPTER_ID
    source_language: str = "en"
    target_language: str = "vi"
    endpoint: str = GOOGLE_BASIC_V2_ENDPOINT
    model: str = GOOGLE_NMT_MODEL
    html_envelope_version: str = "google_translate_html_blocks_v1"
    max_request_characters: int = 5_000
    hard_source_character_cap: int = 170_000
    timeout_seconds: int = 30
    key_bucket_id: str = "google-cloud-translation-banana-key-v1"
    profile_id: str = "evaluation.google_nmt.basic_v2.mlp.v1"


@dataclass(frozen=True, slots=True)
class GoogleTranslateRunPathsV1:
    root: Path
    plan_path: Path
    checkpoint_path: Path
    capture_path: Path


TransportV1 = Callable[[str, GoogleTranslateConfigV1], Mapping[str, Any]]


def build_google_translate_source_input_v1(
    source_db: Path,
    *,
    chapter_id: str,
    created_at: str,
    producer_code_commit: str,
    project_id: str = "d2l",
) -> dict[str, Any]:
    """Build a sealed, source-only input without claiming translation-run authority."""

    database = source_db.resolve()
    if not database.is_file():
        raise GoogleTranslateBaselineError(f"source database does not exist: {database}")
    _require_rfc3339(created_at, "$.created_at")
    _require_commit(producer_code_commit, "$.producer.code_commit")
    _string(chapter_id, "$.identity.selected_chapter_ids[0]")
    _string(project_id, "$.identity.project_id")
    source_db_sha256 = _sha256_file(database)

    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        documents = connection.execute(
            "SELECT doc_id, source_lang, target_lang FROM documents ORDER BY doc_id"
        ).fetchall()
        if len(documents) != 1:
            raise GoogleTranslateBaselineError("source database must contain one document")
        document_id = str(documents[0]["doc_id"] or "")
        if not document_id:
            raise GoogleTranslateBaselineError("source document ID is empty")
        if str(documents[0]["source_lang"] or "") != "en":
            raise GoogleTranslateBaselineError("source document language is not English")
        if str(documents[0]["target_lang"] or "") != "vi":
            raise GoogleTranslateBaselineError("target document language is not Vietnamese")
        rows = connection.execute(
            """
            SELECT block_id, doc_id, order_index, block_type, chapter_id,
                   text, original_text
            FROM blocks
            WHERE chapter_id = ?
            ORDER BY order_index, block_id
            """,
            (chapter_id,),
        ).fetchall()
    finally:
        connection.close()
    if _sha256_file(database) != source_db_sha256:
        raise GoogleTranslateBaselineError("source database changed while being projected")
    if not rows:
        raise GoogleTranslateBaselineError(f"selected chapter has no blocks: {chapter_id}")

    blocks: list[dict[str, Any]] = []
    for row in rows:
        if str(row["doc_id"]) != document_id or str(row["chapter_id"]) != chapter_id:
            raise GoogleTranslateBaselineError("foreign source block in chapter projection")
        source_text = row["original_text"] if row["original_text"] is not None else row["text"]
        if not isinstance(source_text, str) or not source_text:
            raise GoogleTranslateBaselineError(f"empty source block: {row['block_id']}")
        block_type = str(row["block_type"] or "")
        if not block_type:
            raise GoogleTranslateBaselineError(f"empty block type: {row['block_id']}")
        blocks.append(
            {
                "block_id": str(row["block_id"]),
                "chapter_id": chapter_id,
                "order_index": int(row["order_index"]),
                "block_type": block_type,
                "source_text": source_text,
                "admission": (
                    "translate"
                    if block_type in SOURCE_ADMISSION_POLICY["translate_block_types"]
                    else "preserve"
                ),
            }
        )
    block_ids = [row["block_id"] for row in blocks]
    if len(block_ids) != len(set(block_ids)):
        raise GoogleTranslateBaselineError("source block IDs are duplicated")

    admission_manifest = {
        **copy.deepcopy(SOURCE_ADMISSION_POLICY),
        "chapter_id": chapter_id,
        "source_db_sha256": source_db_sha256,
    }
    runtime_manifest_sha256 = _sha256_json(admission_manifest)
    payload = {
        "schema_id": SOURCE_INPUT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "package_id": f"google-source-{source_db_sha256[:16]}-{_sha256_text(chapter_id)[:12]}",
        "created_at": created_at,
        "producer": {
            "workstream": "evaluation",
            "component": "google_translate_source_input_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": producer_code_commit,
        },
        "identity": {
            "project_id": project_id,
            "document_id": document_id,
            "selected_chapter_ids": [chapter_id],
            "source_db_sha256": source_db_sha256,
            "runtime_manifest_sha256": runtime_manifest_sha256,
        },
        "admission_manifest": admission_manifest,
        "blocks": blocks,
        "integrity": {"package_sha256": "0" * 64},
    }
    return validate_google_translate_source_input_v1(
        _seal(payload, ("integrity", "package_sha256"))
    )


def validate_google_translate_source_input_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = _mapping(payload, "$")
    _exact_keys(
        root,
        {
            "schema_id",
            "schema_version",
            "package_id",
            "created_at",
            "producer",
            "identity",
            "admission_manifest",
            "blocks",
            "integrity",
        },
        "$",
    )
    if root["schema_id"] != SOURCE_INPUT_SCHEMA_ID or root["schema_version"] != SCHEMA_VERSION:
        raise ContractValidationError("schema", "$.schema_id", "foreign source input schema")
    _string(root["package_id"], "$.package_id")
    _require_rfc3339(root["created_at"], "$.created_at")
    producer = _mapping(root["producer"], "$.producer")
    _exact_keys(
        producer,
        {"workstream", "component", "component_version", "code_commit"},
        "$.producer",
    )
    if (
        producer["workstream"] != "evaluation"
        or producer["component"] != "google_translate_source_input_v1"
        or producer["component_version"] != SCHEMA_VERSION
    ):
        raise ContractValidationError("producer", "$.producer", "foreign source producer")
    _require_commit(producer["code_commit"], "$.producer.code_commit")

    identity = _mapping(root["identity"], "$.identity")
    _exact_keys(
        identity,
        {
            "project_id",
            "document_id",
            "selected_chapter_ids",
            "source_db_sha256",
            "runtime_manifest_sha256",
        },
        "$.identity",
    )
    _string(identity["project_id"], "$.identity.project_id")
    _string(identity["document_id"], "$.identity.document_id")
    chapters = _list(identity["selected_chapter_ids"], "$.identity.selected_chapter_ids")
    if len(chapters) != 1:
        raise ContractValidationError(
            "chapter_coverage", "$.identity.selected_chapter_ids", "exactly one chapter is required"
        )
    chapter_id = _string(chapters[0], "$.identity.selected_chapter_ids[0]")
    _require_sha256(identity["source_db_sha256"], "$.identity.source_db_sha256")
    _require_sha256(identity["runtime_manifest_sha256"], "$.identity.runtime_manifest_sha256")

    manifest = _mapping(root["admission_manifest"], "$.admission_manifest")
    _exact_keys(
        manifest,
        {
            "policy_id",
            "policy_version",
            "translate_block_types",
            "all_other_block_types",
            "chapter_id",
            "source_db_sha256",
        },
        "$.admission_manifest",
    )
    if manifest != {
        **SOURCE_ADMISSION_POLICY,
        "chapter_id": chapter_id,
        "source_db_sha256": identity["source_db_sha256"],
    }:
        raise ContractValidationError("admission_policy", "$.admission_manifest", "policy drift")
    if _sha256_json(manifest) != identity["runtime_manifest_sha256"]:
        raise ContractValidationError(
            "manifest_hash", "$.identity.runtime_manifest_sha256", "admission manifest hash drift"
        )

    blocks = _list(root["blocks"], "$.blocks")
    if not blocks:
        raise ContractValidationError("empty_array", "$.blocks", "source blocks are required")
    normalized_blocks: list[dict[str, Any]] = []
    previous_order: int | None = None
    for index, raw_block in enumerate(blocks):
        path = f"$.blocks[{index}]"
        block = _mapping(raw_block, path)
        _exact_keys(
            block,
            {"block_id", "chapter_id", "order_index", "block_type", "source_text", "admission"},
            path,
        )
        if block["chapter_id"] != chapter_id:
            raise ContractValidationError("chapter_reference", f"{path}.chapter_id", "foreign chapter")
        order_index = _require_nonnegative_int(block["order_index"], f"{path}.order_index")
        if previous_order is not None and order_index <= previous_order:
            raise ContractValidationError("block_order", f"{path}.order_index", "order must increase")
        previous_order = order_index
        block_type = _string(block["block_type"], f"{path}.block_type")
        expected_admission = (
            "translate"
            if block_type in SOURCE_ADMISSION_POLICY["translate_block_types"]
            else "preserve"
        )
        if block["admission"] != expected_admission:
            raise ContractValidationError("admission", f"{path}.admission", "admission drift")
        normalized_blocks.append(copy.deepcopy(dict(block)))
    ids = [row["block_id"] for row in normalized_blocks]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ContractValidationError("duplicate", "$.blocks.block_id", "invalid or duplicate block ID")
    if any(not isinstance(row["source_text"], str) or not row["source_text"] for row in normalized_blocks):
        raise ContractValidationError("source_text", "$.blocks", "empty source text")

    integrity = _mapping(root["integrity"], "$.integrity")
    _exact_keys(integrity, {"package_sha256"}, "$.integrity")
    _require_sha256(integrity["package_sha256"], "$.integrity.package_sha256")
    if not _verify_seal(root, ("integrity", "package_sha256")):
        raise ContractValidationError("package_hash", "$.integrity.package_sha256", "hash drift")
    return copy.deepcopy(dict(root))


def _validate_google_source_package(payload: Mapping[str, Any]) -> dict[str, Any]:
    schema_id = payload.get("schema_id") if isinstance(payload, Mapping) else None
    if schema_id == SOURCE_INPUT_SCHEMA_ID:
        return validate_google_translate_source_input_v1(payload)
    return validate_d2l_evaluation_input(payload)


def build_google_translate_plan_v1(
    package_payload: Mapping[str, Any],
    *,
    package_file_sha256: str,
    config: GoogleTranslateConfigV1,
    logical_run_id: str,
    attempt_run_id: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    package = _validate_google_source_package(package_payload)
    _require_sha256(package_file_sha256, "$.source.package_file_sha256")
    _require_commit(producer_code_commit, "$.producer.code_commit")
    _require_rfc3339(created_at, "$.created_at")
    if package["identity"]["selected_chapter_ids"] != [config.chapter_id]:
        raise GoogleTranslateBaselineError(
            "source package must select exactly the configured chapter"
        )
    if config.endpoint != GOOGLE_BASIC_V2_ENDPOINT or config.model != GOOGLE_NMT_MODEL:
        raise GoogleTranslateBaselineError(
            "v1 is sealed to official Google Cloud Translation Basic v2 NMT"
        )
    if config.max_request_characters <= 0:
        raise GoogleTranslateBaselineError("max_request_characters must be positive")
    if config.hard_source_character_cap <= 0:
        raise GoogleTranslateBaselineError("hard_source_character_cap must be positive")
    if config.timeout_seconds <= 0:
        raise GoogleTranslateBaselineError("timeout_seconds must be positive")

    ordered_blocks = list(package["blocks"])
    if any(row["chapter_id"] != config.chapter_id for row in ordered_blocks):
        raise GoogleTranslateBaselineError("foreign chapter block in selected package")

    marker_by_block_id: dict[str, str] = {}
    translated_ordinal = 0
    for block in ordered_blocks:
        if block["admission"] == "translate":
            translated_ordinal += 1
            marker_by_block_id[block["block_id"]] = f"b{translated_ordinal:06d}"

    chunks: list[dict[str, Any]] = []
    current: list[dict[str, str]] = []
    current_payload = ""

    def flush() -> None:
        nonlocal current, current_payload
        if not current:
            return
        request_sha256 = _sha256_text(current_payload)
        chunk_index = len(chunks) + 1
        chunks.append(
            {
                "chunk_id": f"gtr-chunk-{chunk_index:04d}-{request_sha256[:16]}",
                "block_refs": copy.deepcopy(current),
                "request_character_count": len(current_payload),
                "request_sha256": request_sha256,
            }
        )
        current = []
        current_payload = ""

    for block in ordered_blocks:
        if block["admission"] != "translate":
            flush()
            continue
        marker_id = marker_by_block_id[block["block_id"]]
        envelope = _render_block_envelope(marker_id, block["source_text"])
        if len(envelope) > config.max_request_characters:
            raise GoogleTranslateBaselineError(
                f"single block exceeds request cap: {block['block_id']}"
            )
        if current and len(current_payload) + len(envelope) > config.max_request_characters:
            flush()
        current.append({"block_id": block["block_id"], "marker_id": marker_id})
        current_payload += envelope
    flush()

    translate_count = len(marker_by_block_id)
    preserve_count = sum(row["admission"] == "preserve" for row in ordered_blocks)
    unsupported = sorted(
        {row["admission"] for row in ordered_blocks} - {"translate", "preserve"}
    )
    if unsupported:
        raise GoogleTranslateBaselineError(
            "v1 accepts only translate/preserve admissions: " + ",".join(unsupported)
        )
    planned_characters = sum(row["request_character_count"] for row in chunks)
    if planned_characters > config.hard_source_character_cap:
        raise GoogleTranslateBaselineError(
            f"planned source characters {planned_characters} exceed hard cap "
            f"{config.hard_source_character_cap}"
        )

    profile_payload = _config_payload(config)
    profile_config_sha256 = _sha256_json(profile_payload)
    plan = {
        "schema_id": PLAN_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "plan_id": f"google-nmt-plan-{package['integrity']['package_sha256'][:16]}",
        "created_at": created_at,
        "producer": {
            "workstream": "evaluation",
            "component": "google_translate_baseline_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": producer_code_commit,
        },
        "source": {
            "package_id": package["package_id"],
            "package_sha256": package["integrity"]["package_sha256"],
            "package_file_sha256": package_file_sha256,
            "project_id": package["identity"]["project_id"],
            "document_id": package["identity"]["document_id"],
            "chapter_id": config.chapter_id,
            "source_db_sha256": package["identity"]["source_db_sha256"],
            "runtime_manifest_sha256": package["identity"]["runtime_manifest_sha256"],
        },
        "run_identity": {
            "logical_run_id": logical_run_id,
            "attempt_run_id": attempt_run_id,
            "arm_id": "google_nmt",
            "profile_id": config.profile_id,
            "profile_config_sha256": profile_config_sha256,
            "key_bucket_id": config.key_bucket_id,
        },
        "profile": profile_payload,
        "coverage_plan": {
            "source_block_count": len(ordered_blocks),
            "translate_block_count": translate_count,
            "preserve_block_count": preserve_count,
            "chunk_count": len(chunks),
            "planned_source_character_count": planned_characters,
        },
        "chunks": chunks,
        "integrity": {"plan_sha256": "0" * 64},
    }
    return validate_google_translate_plan_v1(_seal(plan, ("integrity", "plan_sha256")))


def validate_google_translate_plan_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = _mapping(payload, "$")
    _exact_keys(
        root,
        {
            "schema_id",
            "schema_version",
            "plan_id",
            "created_at",
            "producer",
            "source",
            "run_identity",
            "profile",
            "coverage_plan",
            "chunks",
            "integrity",
        },
        "$",
    )
    if root["schema_id"] != PLAN_SCHEMA_ID or root["schema_version"] != SCHEMA_VERSION:
        raise ContractValidationError("schema", "$", "foreign Google baseline plan")
    _require_rfc3339(root["created_at"], "$.created_at")
    producer = _mapping(root["producer"], "$.producer")
    _exact_keys(
        producer,
        {"workstream", "component", "component_version", "code_commit"},
        "$.producer",
    )
    if producer["workstream"] != "evaluation" or producer["component"] != "google_translate_baseline_v1":
        raise ContractValidationError("producer", "$.producer", "foreign producer")
    _require_commit(producer["code_commit"], "$.producer.code_commit")

    source = _mapping(root["source"], "$.source")
    _exact_keys(
        source,
        {
            "package_id",
            "package_sha256",
            "package_file_sha256",
            "project_id",
            "document_id",
            "chapter_id",
            "source_db_sha256",
            "runtime_manifest_sha256",
        },
        "$.source",
    )
    for name in (
        "package_sha256",
        "package_file_sha256",
        "source_db_sha256",
        "runtime_manifest_sha256",
    ):
        _require_sha256(source[name], f"$.source.{name}")

    run_identity = _mapping(root["run_identity"], "$.run_identity")
    _exact_keys(
        run_identity,
        {
            "logical_run_id",
            "attempt_run_id",
            "arm_id",
            "profile_id",
            "profile_config_sha256",
            "key_bucket_id",
        },
        "$.run_identity",
    )
    if run_identity["arm_id"] != "google_nmt":
        raise ContractValidationError("arm_id", "$.run_identity.arm_id", "foreign arm")
    _require_sha256(
        run_identity["profile_config_sha256"],
        "$.run_identity.profile_config_sha256",
    )

    profile = _validate_profile(root["profile"])
    if _sha256_json(profile) != run_identity["profile_config_sha256"]:
        raise ContractValidationError(
            "profile_hash", "$.run_identity.profile_config_sha256", "profile drift"
        )
    coverage = _mapping(root["coverage_plan"], "$.coverage_plan")
    _exact_keys(
        coverage,
        {
            "source_block_count",
            "translate_block_count",
            "preserve_block_count",
            "chunk_count",
            "planned_source_character_count",
        },
        "$.coverage_plan",
    )
    for name, value in coverage.items():
        _require_nonnegative_int(value, f"$.coverage_plan.{name}")
    if coverage["source_block_count"] != coverage["translate_block_count"] + coverage["preserve_block_count"]:
        raise ContractValidationError("coverage", "$.coverage_plan", "counts do not reconcile")

    chunks = _list(root["chunks"], "$.chunks")
    if len(chunks) != coverage["chunk_count"]:
        raise ContractValidationError("chunk_count", "$.chunks", "chunk count drift")
    block_ids: list[str] = []
    markers: list[str] = []
    character_sum = 0
    for index, raw_chunk in enumerate(chunks):
        path = f"$.chunks[{index}]"
        chunk = _mapping(raw_chunk, path)
        _exact_keys(
            chunk,
            {
                "chunk_id",
                "block_refs",
                "request_character_count",
                "request_sha256",
            },
            path,
        )
        _require_sha256(chunk["request_sha256"], f"{path}.request_sha256")
        count = _require_nonnegative_int(
            chunk["request_character_count"], f"{path}.request_character_count"
        )
        if count <= 0 or count > profile["max_request_characters"]:
            raise ContractValidationError("request_size", path, "chunk exceeds sealed cap")
        refs = _list(chunk["block_refs"], f"{path}.block_refs")
        if not refs:
            raise ContractValidationError("empty_array", f"{path}.block_refs", "empty chunk")
        for ref_index, raw_ref in enumerate(refs):
            ref_path = f"{path}.block_refs[{ref_index}]"
            ref = _mapping(raw_ref, ref_path)
            _exact_keys(ref, {"block_id", "marker_id"}, ref_path)
            block_ids.append(_string(ref["block_id"], f"{ref_path}.block_id"))
            markers.append(_string(ref["marker_id"], f"{ref_path}.marker_id"))
        character_sum += count
    if len(block_ids) != len(set(block_ids)) or len(markers) != len(set(markers)):
        raise ContractValidationError("duplicate", "$.chunks", "duplicate block or marker")
    if len(block_ids) != coverage["translate_block_count"]:
        raise ContractValidationError("coverage", "$.chunks", "translate coverage drift")
    if character_sum != coverage["planned_source_character_count"]:
        raise ContractValidationError("coverage", "$.chunks", "character count drift")
    if character_sum > profile["hard_source_character_cap"]:
        raise ContractValidationError("hard_cap", "$.chunks", "plan exceeds hard cap")
    integrity = _mapping(root["integrity"], "$.integrity")
    _exact_keys(integrity, {"plan_sha256"}, "$.integrity")
    _require_sha256(integrity["plan_sha256"], "$.integrity.plan_sha256")
    if not _verify_seal(root, ("integrity", "plan_sha256")):
        raise ContractValidationError("plan_hash", "$.integrity.plan_sha256", "hash drift")
    return copy.deepcopy(dict(root))


def execute_google_translate_plan_v1(
    package_payload: Mapping[str, Any],
    *,
    plan_payload: Mapping[str, Any],
    output_root: Path,
    transport: TransportV1,
    resume: bool,
) -> GoogleTranslateRunPathsV1:
    package = _validate_google_source_package(package_payload)
    plan = validate_google_translate_plan_v1(plan_payload)
    _validate_plan_package_binding(plan, package)
    config = _config_from_plan(plan)
    paths = _prepare_paths(output_root)
    _write_immutable_json(paths.plan_path, plan)

    if paths.checkpoint_path.exists():
        if not resume:
            raise GoogleTranslateBaselineError(
                "checkpoint exists; resume must be explicit"
            )
        checkpoint = validate_google_translate_checkpoint_v1(
            _load_json(paths.checkpoint_path)
        )
        if checkpoint["plan_sha256"] != plan["integrity"]["plan_sha256"]:
            raise GoogleTranslateBaselineError("checkpoint belongs to a different plan")
    else:
        checkpoint = _new_checkpoint(plan)
        _write_checkpoint(paths.checkpoint_path, checkpoint)

    if checkpoint["status"] == "complete":
        if not paths.capture_path.exists():
            raise GoogleTranslateBaselineError("complete checkpoint has no capture artifact")
        capture = validate_google_translate_capture_v1(_load_json(paths.capture_path))
        if capture["integrity"]["capture_sha256"] != checkpoint["capture_sha256"]:
            raise GoogleTranslateBaselineError("complete checkpoint/capture binding drift")
        return paths

    checkpoint, recovered_chunks = _recover_persisted_responses(
        checkpoint,
        plan=plan,
        root=paths.root,
    )
    if recovered_chunks:
        _write_checkpoint(paths.checkpoint_path, checkpoint)
        for chunk_id in recovered_chunks:
            _write_immutable_json(
                paths.root / "recoveries" / f"{chunk_id}.json",
                {
                    "schema_id": "GoogleTranslateCheckpointRecoveryReceiptV1",
                    "schema_version": SCHEMA_VERSION,
                    "plan_sha256": plan["integrity"]["plan_sha256"],
                    "recovered_chunk_id": chunk_id,
                    "recovery_basis": "validated_immutable_response_record",
                    "provider_calls_added": 0,
                },
            )

    completed = {
        row["chunk_id"]: row for row in checkpoint["completed_chunks"]
    }
    unresolved = [
        row for row in checkpoint["attempts"] if row["status"] != "succeeded"
    ]
    if unresolved:
        raise GoogleTranslateBaselineError(
            "checkpoint contains an unresolved physical attempt; automatic retry is forbidden"
        )

    for chunk in plan["chunks"]:
        if chunk["chunk_id"] in completed:
            continue
        request_text = _render_chunk_from_package(package, chunk)
        if len(request_text) != chunk["request_character_count"]:
            raise GoogleTranslateBaselineError("request character count drift")
        if _sha256_text(request_text) != chunk["request_sha256"]:
            raise GoogleTranslateBaselineError("request hash drift")
        next_total = checkpoint["usage"]["reserved_source_character_count"] + len(request_text)
        if next_total > config.hard_source_character_cap:
            raise GoogleTranslateBaselineError("hard source-character cap would be exceeded")

        request_record = {
            "chunk_id": chunk["chunk_id"],
            "endpoint": config.endpoint,
            "model": config.model,
            "source_language": config.source_language,
            "target_language": config.target_language,
            "format": "html",
            "request_character_count": len(request_text),
            "request_sha256": chunk["request_sha256"],
            "q": request_text,
        }
        request_path = paths.root / "requests" / f"{chunk['chunk_id']}.json"
        _write_immutable_json(request_path, request_record)

        attempt = {
            "chunk_id": chunk["chunk_id"],
            "attempt_index": 1,
            "status": "pending_unknown",
            "request_character_count": len(request_text),
            "request_sha256": chunk["request_sha256"],
            "response_sha256": None,
            "http_status": None,
            "error_code": None,
            "outcome_known": False,
        }
        checkpoint["attempts"].append(attempt)
        checkpoint["usage"]["physical_request_count"] += 1
        checkpoint["usage"]["reserved_source_character_count"] = next_total
        checkpoint["status"] = "running"
        checkpoint = _reseal_checkpoint(checkpoint)
        _write_checkpoint(paths.checkpoint_path, checkpoint)
        attempt = checkpoint["attempts"][-1]

        try:
            response = dict(transport(request_text, config))
            translated_html = _extract_provider_translation(response)
            targets = parse_google_translated_html_v1(
                translated_html,
                expected_markers=[ref["marker_id"] for ref in chunk["block_refs"]],
            )
        except GoogleTranslateTransportError as exc:
            attempt["status"] = "failed_known" if exc.outcome_known else "pending_unknown"
            attempt["http_status"] = exc.http_status
            attempt["error_code"] = exc.error_code
            attempt["outcome_known"] = exc.outcome_known
            checkpoint["status"] = (
                "halted_failed" if exc.outcome_known else "halted_pending_unknown"
            )
            checkpoint = _reseal_checkpoint(checkpoint)
            _write_checkpoint(paths.checkpoint_path, checkpoint)
            raise
        except Exception as exc:
            attempt["status"] = "failed_known"
            attempt["error_code"] = "response_contract_failure"
            attempt["http_status"] = 200
            attempt["outcome_known"] = True
            checkpoint["status"] = "halted_failed"
            checkpoint = _reseal_checkpoint(checkpoint)
            _write_checkpoint(paths.checkpoint_path, checkpoint)
            raise GoogleTranslateBaselineError(
                f"provider response failed local validation: {type(exc).__name__}"
            ) from exc

        response_record = {
            "chunk_id": chunk["chunk_id"],
            "provider_response": response,
            "translated_html_sha256": _sha256_text(translated_html),
            "translations": [
                {
                    "block_id": ref["block_id"],
                    "marker_id": ref["marker_id"],
                    "target_text": targets[ref["marker_id"]],
                }
                for ref in chunk["block_refs"]
            ],
        }
        response_sha256 = _sha256_json(response_record)
        response_record["response_record_sha256"] = response_sha256
        response_path = paths.root / "responses" / f"{chunk['chunk_id']}.json"
        _write_immutable_json(response_path, response_record)

        attempt["status"] = "succeeded"
        attempt["response_sha256"] = response_sha256
        attempt["http_status"] = 200
        attempt["outcome_known"] = True
        checkpoint["completed_chunks"].append(
            {
                "chunk_id": chunk["chunk_id"],
                "request_sha256": chunk["request_sha256"],
                "response_sha256": response_sha256,
            }
        )
        checkpoint["usage"]["completed_source_character_count"] += len(request_text)
        checkpoint["status"] = "running"
        checkpoint = _reseal_checkpoint(checkpoint)
        _write_checkpoint(paths.checkpoint_path, checkpoint)

    capture = _build_capture(package, plan, checkpoint, paths.root)
    _write_immutable_json(paths.capture_path, capture)
    checkpoint["status"] = "complete"
    checkpoint["capture_sha256"] = capture["integrity"]["capture_sha256"]
    checkpoint = _reseal_checkpoint(checkpoint)
    _write_checkpoint(paths.checkpoint_path, checkpoint)
    return paths


def parse_google_translated_html_v1(
    translated_html: str, *, expected_markers: Sequence[str]
) -> dict[str, str]:
    parser = _BlockEnvelopeParser()
    parser.feed(translated_html)
    parser.close()
    if parser.outside_text.strip():
        raise GoogleTranslateBaselineError("unexpected text outside block envelopes")
    if parser.markers != list(expected_markers):
        raise GoogleTranslateBaselineError("translated block markers/order drift")
    result: dict[str, str] = {}
    for marker, parts in parser.content.items():
        lines = "".join(parts).split("\n")
        target = "\n".join(line.strip() for line in lines).strip()
        if not target:
            raise GoogleTranslateBaselineError(f"empty translation for marker {marker}")
        result[marker] = target
    return result


def validate_google_translate_checkpoint_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = _mapping(payload, "$")
    _exact_keys(
        root,
        {
            "schema_id",
            "schema_version",
            "plan_sha256",
            "status",
            "attempts",
            "completed_chunks",
            "usage",
            "capture_sha256",
            "integrity",
        },
        "$",
    )
    if root["schema_id"] != CHECKPOINT_SCHEMA_ID or root["schema_version"] != SCHEMA_VERSION:
        raise ContractValidationError("schema", "$", "foreign checkpoint")
    _require_sha256(root["plan_sha256"], "$.plan_sha256")
    if root["capture_sha256"] is not None:
        _require_sha256(root["capture_sha256"], "$.capture_sha256")
    if root["status"] not in {
        "ready",
        "running",
        "halted_failed",
        "halted_pending_unknown",
        "complete",
    }:
        raise ContractValidationError("status", "$.status", "invalid checkpoint status")
    attempts = _list(root["attempts"], "$.attempts")
    seen_attempt_chunks: set[str] = set()
    reserved = 0
    completed_chars = 0
    for index, raw in enumerate(attempts):
        path = f"$.attempts[{index}]"
        row = _mapping(raw, path)
        _exact_keys(
            row,
            {
                "chunk_id",
                "attempt_index",
                "status",
                "request_character_count",
                "request_sha256",
                "response_sha256",
                "http_status",
                "error_code",
                "outcome_known",
            },
            path,
        )
        chunk_id = _string(row["chunk_id"], f"{path}.chunk_id")
        if chunk_id in seen_attempt_chunks or row["attempt_index"] != 1:
            raise ContractValidationError("attempt", path, "v1 permits one attempt per chunk")
        seen_attempt_chunks.add(chunk_id)
        count = _require_nonnegative_int(
            row["request_character_count"], f"{path}.request_character_count"
        )
        reserved += count
        _require_sha256(row["request_sha256"], f"{path}.request_sha256")
        if row["status"] not in {"pending_unknown", "failed_known", "succeeded"}:
            raise ContractValidationError("status", f"{path}.status", "invalid attempt status")
        if row["status"] == "succeeded":
            _require_sha256(row["response_sha256"], f"{path}.response_sha256")
            if row["http_status"] != 200 or row["error_code"] is not None or row["outcome_known"] is not True:
                raise ContractValidationError("attempt", path, "invalid successful attempt")
            completed_chars += count
        elif row["response_sha256"] is not None:
            raise ContractValidationError("attempt", path, "failed attempt has response hash")

    completed = _list(root["completed_chunks"], "$.completed_chunks")
    completed_ids: set[str] = set()
    succeeded = {row["chunk_id"]: row for row in attempts if row["status"] == "succeeded"}
    for index, raw in enumerate(completed):
        path = f"$.completed_chunks[{index}]"
        row = _mapping(raw, path)
        _exact_keys(row, {"chunk_id", "request_sha256", "response_sha256"}, path)
        chunk_id = _string(row["chunk_id"], f"{path}.chunk_id")
        if chunk_id in completed_ids or chunk_id not in succeeded:
            raise ContractValidationError("completed_chunk", path, "invalid completion row")
        completed_ids.add(chunk_id)
        if row["request_sha256"] != succeeded[chunk_id]["request_sha256"] or row["response_sha256"] != succeeded[chunk_id]["response_sha256"]:
            raise ContractValidationError("completed_chunk", path, "attempt binding drift")

    usage = _mapping(root["usage"], "$.usage")
    _exact_keys(
        usage,
        {
            "physical_request_count",
            "reserved_source_character_count",
            "completed_source_character_count",
            "provider_reported_cost_usd",
        },
        "$.usage",
    )
    if usage["physical_request_count"] != len(attempts):
        raise ContractValidationError("usage", "$.usage", "request count drift")
    if usage["reserved_source_character_count"] != reserved or usage["completed_source_character_count"] != completed_chars:
        raise ContractValidationError("usage", "$.usage", "character accounting drift")
    if usage["provider_reported_cost_usd"] is not None:
        raise ContractValidationError("cost", "$.usage.provider_reported_cost_usd", "provider supplied no exact call cost")
    integrity = _mapping(root["integrity"], "$.integrity")
    _exact_keys(integrity, {"checkpoint_sha256"}, "$.integrity")
    _require_sha256(integrity["checkpoint_sha256"], "$.integrity.checkpoint_sha256")
    if not _verify_seal(root, ("integrity", "checkpoint_sha256")):
        raise ContractValidationError("checkpoint_hash", "$.integrity", "hash drift")
    return copy.deepcopy(dict(root))


def validate_google_translate_capture_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = _mapping(payload, "$")
    _exact_keys(
        root,
        {
            "schema_id",
            "schema_version",
            "capture_id",
            "created_at",
            "producer",
            "source",
            "run_identity",
            "translations",
            "coverage",
            "chunks",
            "usage",
            "authority",
            "integrity",
        },
        "$",
    )
    if root["schema_id"] != CAPTURE_SCHEMA_ID or root["schema_version"] != SCHEMA_VERSION:
        raise ContractValidationError("schema", "$", "foreign capture")
    if root["producer"]["workstream"] != "evaluation":
        raise ContractValidationError("producer", "$.producer", "foreign capture producer")
    if root["authority"] != {
        "artifact_kind": "evaluation_private_baseline_capture",
        "public_translation_artifact": False,
        "requires_producer_promotion": True,
    }:
        raise ContractValidationError("authority", "$.authority", "authority drift")
    rows = _list(root["translations"], "$.translations")
    block_ids = [row["block_id"] for row in rows]
    if len(block_ids) != len(set(block_ids)):
        raise ContractValidationError("duplicate", "$.translations", "duplicate block")
    counts = {"translated": 0, "preserved": 0}
    for index, row in enumerate(rows):
        path = f"$.translations[{index}]"
        _exact_keys(row, {"block_id", "status", "target_text", "error_code"}, path)
        if row["status"] not in counts:
            raise ContractValidationError("status", f"{path}.status", "invalid capture status")
        if not isinstance(row["target_text"], str) or not row["target_text"]:
            raise ContractValidationError("target", f"{path}.target_text", "missing text")
        if row["error_code"] is not None:
            raise ContractValidationError("error", f"{path}.error_code", "complete capture has error")
        counts[row["status"]] += 1
    coverage = root["coverage"]
    if coverage != {
        "source_block_count": len(rows),
        "translated_count": counts["translated"],
        "preserved_count": counts["preserved"],
        "missing_count": 0,
        "failed_count": 0,
    }:
        raise ContractValidationError("coverage", "$.coverage", "coverage drift")
    integrity = _mapping(root["integrity"], "$.integrity")
    _exact_keys(integrity, {"capture_sha256"}, "$.integrity")
    _require_sha256(integrity["capture_sha256"], "$.integrity.capture_sha256")
    if not _verify_seal(root, ("integrity", "capture_sha256")):
        raise ContractValidationError("capture_hash", "$.integrity", "hash drift")
    return copy.deepcopy(dict(root))


def google_basic_v2_transport_v1(
    api_key: str,
) -> TransportV1:
    secret = api_key.strip()
    if not secret:
        raise GoogleTranslateBaselineError("empty Cloud Translation API key")

    def send(request_text: str, config: GoogleTranslateConfigV1) -> Mapping[str, Any]:
        body = {
            "q": request_text,
            "source": config.source_language,
            "target": config.target_language,
            "format": "html",
            "model": config.model,
        }
        request = Request(
            config.endpoint,
            data=_json_bytes(body),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Goog-Api-Key": secret,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=config.timeout_seconds) as response:
                raw = response.read()
                if response.status != 200:
                    raise GoogleTranslateTransportError(
                        "Cloud Translation returned a non-200 response",
                        error_code="http_error",
                        outcome_known=True,
                        http_status=response.status,
                    )
        except HTTPError as exc:
            raise GoogleTranslateTransportError(
                f"Cloud Translation HTTP {exc.code}",
                error_code=f"http_{exc.code}",
                outcome_known=True,
                http_status=exc.code,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise GoogleTranslateTransportError(
                "Cloud Translation request timed out",
                error_code="timeout_unknown_outcome",
                outcome_known=False,
            ) from exc
        except URLError as exc:
            raise GoogleTranslateTransportError(
                "Cloud Translation network failure",
                error_code="network_unknown_outcome",
                outcome_known=False,
            ) from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleTranslateTransportError(
                "Cloud Translation returned invalid JSON",
                error_code="invalid_json",
                outcome_known=True,
                http_status=200,
            ) from exc
        if not isinstance(parsed, dict):
            raise GoogleTranslateTransportError(
                "Cloud Translation returned a non-object payload",
                error_code="invalid_response_shape",
                outcome_known=True,
                http_status=200,
            )
        return parsed

    return send


def _build_capture(
    package: Mapping[str, Any],
    plan: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    target_by_block: dict[str, str] = {}
    for chunk in plan["chunks"]:
        response_path = root / "responses" / f"{chunk['chunk_id']}.json"
        response = _load_json(response_path)
        if response["response_record_sha256"] != _sha256_json(
            {key: value for key, value in response.items() if key != "response_record_sha256"}
        ):
            raise GoogleTranslateBaselineError("response record hash drift")
        for row in response["translations"]:
            if row["block_id"] in target_by_block:
                raise GoogleTranslateBaselineError("duplicate translated block")
            target_by_block[row["block_id"]] = row["target_text"]

    translations: list[dict[str, Any]] = []
    for block in package["blocks"]:
        if block["admission"] == "translate":
            target = target_by_block.get(block["block_id"])
            if target is None:
                raise GoogleTranslateBaselineError("capture missing translated block")
            status = "translated"
        else:
            target = block["source_text"]
            status = "preserved"
        translations.append(
            {
                "block_id": block["block_id"],
                "status": status,
                "target_text": target,
                "error_code": None,
            }
        )
    translated_count = len(target_by_block)
    preserved_count = len(translations) - translated_count
    capture = {
        "schema_id": CAPTURE_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "capture_id": f"google-nmt-capture-{plan['integrity']['plan_sha256'][:24]}",
        "created_at": plan["created_at"],
        "producer": copy.deepcopy(plan["producer"]),
        "source": copy.deepcopy(plan["source"]),
        "run_identity": copy.deepcopy(plan["run_identity"]),
        "translations": translations,
        "coverage": {
            "source_block_count": len(translations),
            "translated_count": translated_count,
            "preserved_count": preserved_count,
            "missing_count": 0,
            "failed_count": 0,
        },
        "chunks": copy.deepcopy(checkpoint["completed_chunks"]),
        "usage": copy.deepcopy(checkpoint["usage"]),
        "authority": {
            "artifact_kind": "evaluation_private_baseline_capture",
            "public_translation_artifact": False,
            "requires_producer_promotion": True,
        },
        "integrity": {"capture_sha256": "0" * 64},
    }
    return validate_google_translate_capture_v1(
        _seal(capture, ("integrity", "capture_sha256"))
    )


def _recover_persisted_responses(
    checkpoint: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    root: Path,
) -> tuple[dict[str, Any], list[str]]:
    recovered = copy.deepcopy(dict(checkpoint))
    chunks = {row["chunk_id"]: row for row in plan["chunks"]}
    completed_ids = {row["chunk_id"] for row in recovered["completed_chunks"]}
    recovered_ids: list[str] = []
    for attempt in recovered["attempts"]:
        if attempt["status"] != "pending_unknown":
            continue
        chunk = chunks.get(attempt["chunk_id"])
        if chunk is None:
            raise GoogleTranslateBaselineError("pending attempt references a foreign chunk")
        response_path = root / "responses" / f"{attempt['chunk_id']}.json"
        if not response_path.exists():
            continue
        response = _load_json(response_path)
        recorded_hash = response.get("response_record_sha256")
        if not isinstance(recorded_hash, str) or recorded_hash != _sha256_json(
            {key: value for key, value in response.items() if key != "response_record_sha256"}
        ):
            raise GoogleTranslateBaselineError("persisted response record hash drift")
        rows = response.get("translations")
        if not isinstance(rows, list):
            raise GoogleTranslateBaselineError("persisted response rows are invalid")
        expected = [
            (ref["block_id"], ref["marker_id"]) for ref in chunk["block_refs"]
        ]
        actual: list[tuple[str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise GoogleTranslateBaselineError("persisted response row is invalid")
            if set(row) != {"block_id", "marker_id", "target_text"}:
                raise GoogleTranslateBaselineError("persisted response row keys drift")
            if not isinstance(row["target_text"], str) or not row["target_text"]:
                raise GoogleTranslateBaselineError("persisted response target is empty")
            actual.append((row["block_id"], row["marker_id"]))
        if actual != expected:
            raise GoogleTranslateBaselineError("persisted response block binding drift")
        if attempt["request_sha256"] != chunk["request_sha256"]:
            raise GoogleTranslateBaselineError("pending attempt request binding drift")
        attempt["status"] = "succeeded"
        attempt["response_sha256"] = recorded_hash
        attempt["http_status"] = 200
        attempt["error_code"] = None
        attempt["outcome_known"] = True
        if attempt["chunk_id"] not in completed_ids:
            recovered["completed_chunks"].append(
                {
                    "chunk_id": attempt["chunk_id"],
                    "request_sha256": attempt["request_sha256"],
                    "response_sha256": recorded_hash,
                }
            )
            recovered["usage"]["completed_source_character_count"] += attempt[
                "request_character_count"
            ]
            completed_ids.add(attempt["chunk_id"])
        recovered_ids.append(attempt["chunk_id"])
    if recovered_ids:
        recovered["status"] = "running"
        recovered = _reseal_checkpoint(recovered)
    return recovered, recovered_ids


def _new_checkpoint(plan: Mapping[str, Any]) -> dict[str, Any]:
    return validate_google_translate_checkpoint_v1(
        _seal(
            {
                "schema_id": CHECKPOINT_SCHEMA_ID,
                "schema_version": SCHEMA_VERSION,
                "plan_sha256": plan["integrity"]["plan_sha256"],
                "status": "ready",
                "attempts": [],
                "completed_chunks": [],
                "usage": {
                    "physical_request_count": 0,
                    "reserved_source_character_count": 0,
                    "completed_source_character_count": 0,
                    "provider_reported_cost_usd": None,
                },
                "capture_sha256": None,
                "integrity": {"checkpoint_sha256": "0" * 64},
            },
            ("integrity", "checkpoint_sha256"),
        )
    )


def _reseal_checkpoint(payload: Mapping[str, Any]) -> dict[str, Any]:
    return validate_google_translate_checkpoint_v1(
        _seal(payload, ("integrity", "checkpoint_sha256"))
    )


def _render_chunk_from_package(
    package: Mapping[str, Any], chunk: Mapping[str, Any]
) -> str:
    blocks = {row["block_id"]: row for row in package["blocks"]}
    parts: list[str] = []
    for ref in chunk["block_refs"]:
        block = blocks.get(ref["block_id"])
        if block is None or block["admission"] != "translate":
            raise GoogleTranslateBaselineError("chunk references a foreign/ineligible block")
        parts.append(_render_block_envelope(ref["marker_id"], block["source_text"]))
    return "".join(parts)


def _render_block_envelope(marker_id: str, source_text: str) -> str:
    escaped = html.escape(source_text, quote=False).replace("\n", "<br>")
    return f'<div data-eval-block="{marker_id}">{escaped}</div>'


class _BlockEnvelopeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.active: str | None = None
        self.depth = 0
        self.markers: list[str] = []
        self.content: dict[str, list[str]] = {}
        self.outside_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        marker = attributes.get("data-eval-block") if tag == "div" else None
        if marker is not None:
            if self.active is not None or marker in self.content:
                raise GoogleTranslateBaselineError("nested or duplicate block envelope")
            self.active = marker
            self.depth = 1
            self.markers.append(marker)
            self.content[marker] = []
            return
        if self.active is not None:
            if tag == "br":
                self.content[self.active].append("\n")
                return
            self.depth += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self.active is not None and tag == "br":
            self.content[self.active].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.active is None:
            return
        self.depth -= 1
        if self.depth == 0:
            if tag != "div":
                raise GoogleTranslateBaselineError("block envelope closed by wrong tag")
            self.active = None

    def handle_data(self, data: str) -> None:
        if self.active is None:
            self.outside_text += data
        else:
            self.content[self.active].append(data)

    def close(self) -> None:
        super().close()
        if self.active is not None:
            raise GoogleTranslateBaselineError("unclosed block envelope")


def _extract_provider_translation(response: Mapping[str, Any]) -> str:
    if set(response) != {"data"}:
        raise GoogleTranslateBaselineError("unexpected provider response root")
    data = response["data"]
    if not isinstance(data, Mapping) or set(data) != {"translations"}:
        raise GoogleTranslateBaselineError("unexpected provider response data")
    translations = data["translations"]
    if not isinstance(translations, list) or len(translations) != 1:
        raise GoogleTranslateBaselineError("provider must return exactly one translation")
    row = translations[0]
    if not isinstance(row, Mapping) or set(row) - {"translatedText", "detectedSourceLanguage", "model"}:
        raise GoogleTranslateBaselineError("unexpected provider translation row")
    translated = row.get("translatedText")
    if not isinstance(translated, str) or not translated:
        raise GoogleTranslateBaselineError("provider returned empty translated text")
    return translated


def _validate_plan_package_binding(
    plan: Mapping[str, Any], package: Mapping[str, Any]
) -> None:
    source = plan["source"]
    identity = package["identity"]
    expected = {
        "package_id": package["package_id"],
        "package_sha256": package["integrity"]["package_sha256"],
        "project_id": identity["project_id"],
        "document_id": identity["document_id"],
        "chapter_id": identity["selected_chapter_ids"][0],
        "source_db_sha256": identity["source_db_sha256"],
        "runtime_manifest_sha256": identity["runtime_manifest_sha256"],
    }
    for key, value in expected.items():
        if source[key] != value:
            raise GoogleTranslateBaselineError(f"plan/package binding drift: {key}")
    block_ids = [row["block_id"] for row in package["blocks"] if row["admission"] == "translate"]
    planned_ids = [ref["block_id"] for chunk in plan["chunks"] for ref in chunk["block_refs"]]
    if planned_ids != block_ids:
        raise GoogleTranslateBaselineError("plan/package translate block order drift")


def _config_payload(config: GoogleTranslateConfigV1) -> dict[str, Any]:
    return {
        "chapter_id": config.chapter_id,
        "source_language": config.source_language,
        "target_language": config.target_language,
        "endpoint": config.endpoint,
        "model": config.model,
        "html_envelope_version": config.html_envelope_version,
        "max_request_characters": config.max_request_characters,
        "hard_source_character_cap": config.hard_source_character_cap,
        "timeout_seconds": config.timeout_seconds,
        "transport_attempts_per_chunk": 1,
        "semantic_retries_per_chunk": 0,
        "fallback_policy": "none",
    }


def _validate_profile(value: Any) -> dict[str, Any]:
    row = _mapping(value, "$.profile")
    expected = {
        "chapter_id",
        "source_language",
        "target_language",
        "endpoint",
        "model",
        "html_envelope_version",
        "max_request_characters",
        "hard_source_character_cap",
        "timeout_seconds",
        "transport_attempts_per_chunk",
        "semantic_retries_per_chunk",
        "fallback_policy",
    }
    _exact_keys(row, expected, "$.profile")
    if row["endpoint"] != GOOGLE_BASIC_V2_ENDPOINT or row["model"] != GOOGLE_NMT_MODEL:
        raise ContractValidationError("profile", "$.profile", "foreign provider/model")
    if row["transport_attempts_per_chunk"] != 1 or row["semantic_retries_per_chunk"] != 0 or row["fallback_policy"] != "none":
        raise ContractValidationError("retry", "$.profile", "v1 forbids retries/fallback")
    for name in ("max_request_characters", "hard_source_character_cap", "timeout_seconds"):
        if _require_nonnegative_int(row[name], f"$.profile.{name}") <= 0:
            raise ContractValidationError("positive", f"$.profile.{name}", "must be positive")
    return copy.deepcopy(dict(row))


def _config_from_plan(plan: Mapping[str, Any]) -> GoogleTranslateConfigV1:
    row = plan["profile"]
    return GoogleTranslateConfigV1(
        chapter_id=row["chapter_id"],
        source_language=row["source_language"],
        target_language=row["target_language"],
        endpoint=row["endpoint"],
        model=row["model"],
        html_envelope_version=row["html_envelope_version"],
        max_request_characters=row["max_request_characters"],
        hard_source_character_cap=row["hard_source_character_cap"],
        timeout_seconds=row["timeout_seconds"],
        key_bucket_id=plan["run_identity"]["key_bucket_id"],
        profile_id=plan["run_identity"]["profile_id"],
    )


def _prepare_paths(output_root: Path) -> GoogleTranslateRunPathsV1:
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return GoogleTranslateRunPathsV1(
        root=root,
        plan_path=root / "plan.json",
        checkpoint_path=root / "checkpoint.json",
        capture_path=root / "capture.json",
    )


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    _write_json_atomic(path, payload)


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = _json_bytes(payload) + b"\n"
    if path.exists():
        if path.read_bytes() != encoded:
            raise GoogleTranslateBaselineError(f"immutable artifact conflict: {path}")
        return
    _write_bytes_atomic(path, encoded)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes_atomic(path, _json_bytes(payload) + b"\n")


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt_index in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt_index == 9:
                    raise
                time.sleep(0.05 * (attempt_index + 1))
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GoogleTranslateBaselineError(f"expected JSON object: {path}")
    return value


def _seal(payload: Mapping[str, Any], hash_path: tuple[str, str]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(payload))
    parent = sealed[hash_path[0]]
    if not isinstance(parent, dict):
        raise TypeError("self-hash parent must be an object")
    parent.pop(hash_path[1], None)
    digest = _sha256_json(sealed)
    parent[hash_path[1]] = digest
    return sealed


def _verify_seal(payload: Mapping[str, Any], hash_path: tuple[str, str]) -> bool:
    value = payload.get(hash_path[0])
    if not isinstance(value, Mapping):
        return False
    recorded = value.get(hash_path[1])
    if not isinstance(recorded, str):
        return False
    unhashed = copy.deepcopy(dict(payload))
    parent = unhashed[hash_path[0]]
    if not isinstance(parent, dict):
        return False
    parent.pop(hash_path[1], None)
    return _sha256_json(unhashed) == recorded


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError("type", path, "expected object")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractValidationError("type", path, "expected array")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError("type", path, "expected non-empty string")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ContractValidationError(
            "keys",
            path,
            f"expected={sorted(expected)} actual={sorted(actual)}",
        )


def _require_nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError("type", path, "expected non-negative integer")
    return value


def _require_sha256(value: Any, path: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ContractValidationError("sha256", path, "invalid SHA-256")


def _require_commit(value: Any, path: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise ContractValidationError("commit", path, "invalid Git commit")


def _require_rfc3339(value: Any, path: str) -> None:
    if not isinstance(value, str):
        raise ContractValidationError("timestamp", path, "expected RFC3339 string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractValidationError("timestamp", path, "invalid RFC3339 timestamp") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_package(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise GoogleTranslateBaselineError("package root must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Capture a bounded Google NMT baseline")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--package", type=Path)
    source_group.add_argument("--source-db", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--logical-run-id", required=True)
    parser.add_argument("--attempt-run-id", required=True)
    parser.add_argument("--producer-code-commit", required=True)
    parser.add_argument("--created-at", default=None)
    parser.add_argument("--chapter-id", default=DEFAULT_CHAPTER_ID)
    parser.add_argument("--project-id", default="d2l")
    parser.add_argument("--profile-id", default=None)
    parser.add_argument("--credential-env", default="GOOGLE_TRANSLATE_API_KEY")
    parser.add_argument("--key-bucket-id", default="google-cloud-translation-banana-key-v1")
    parser.add_argument("--max-request-characters", type=int, default=5_000)
    parser.add_argument("--hard-source-character-cap", type=int, default=170_000)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    existing_plan_path = args.output_root.resolve() / "plan.json"
    existing_plan = None
    if args.resume and existing_plan_path.exists():
        existing_plan = validate_google_translate_plan_v1(_load_json(existing_plan_path))
    created_at = (
        args.created_at
        or (existing_plan["created_at"] if existing_plan is not None else _utc_now())
    )
    if args.source_db is not None:
        package = build_google_translate_source_input_v1(
            args.source_db,
            chapter_id=args.chapter_id,
            created_at=created_at,
            producer_code_commit=args.producer_code_commit,
            project_id=args.project_id,
        )
        source_input_path = args.output_root.resolve() / "source_input.json"
        _write_immutable_json(source_input_path, package)
        package_file_sha256 = _sha256_file(source_input_path)
    else:
        if args.package is None:
            raise AssertionError("source group requires package or source database")
        package, package_file_sha256 = _load_package(args.package)
    config = GoogleTranslateConfigV1(
        chapter_id=args.chapter_id,
        max_request_characters=args.max_request_characters,
        hard_source_character_cap=args.hard_source_character_cap,
        timeout_seconds=args.timeout_seconds,
        key_bucket_id=args.key_bucket_id,
        profile_id=(
            args.profile_id
            or (
                "evaluation.google_nmt.basic_v2.mlp.v1"
                if args.chapter_id == DEFAULT_CHAPTER_ID
                else f"evaluation.google_nmt.basic_v2.{args.chapter_id}.v1"
            )
        ),
    )
    plan = build_google_translate_plan_v1(
        package,
        package_file_sha256=package_file_sha256,
        config=config,
        logical_run_id=args.logical_run_id,
        attempt_run_id=args.attempt_run_id,
        created_at=created_at,
        producer_code_commit=args.producer_code_commit,
    )
    if existing_plan is not None and existing_plan != plan:
        raise GoogleTranslateBaselineError(
            "resume parameters or source differ from the sealed plan"
        )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "plan_sha256": plan["integrity"]["plan_sha256"],
                    "coverage_plan": plan["coverage_plan"],
                    "profile_config_sha256": plan["run_identity"]["profile_config_sha256"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    api_key = os.environ.get(args.credential_env, "")
    if not api_key:
        raise GoogleTranslateBaselineError(
            f"credential environment variable is not set: {args.credential_env}"
        )
    paths = execute_google_translate_plan_v1(
        package,
        plan_payload=plan,
        output_root=args.output_root,
        transport=google_basic_v2_transport_v1(api_key),
        resume=args.resume,
    )
    print(
        json.dumps(
            {
                "output_root": str(paths.root),
                "capture_path": str(paths.capture_path),
                "status": "complete",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
