"""Prepare a chapter-selectable, sealed production D2L campaign.

This module is the project-facing layer above the accepted 11-stage component
runner.  It validates an immutable Canonical Source Package, selects chapters
in source order, copies the source database into isolated campaign state, and
seals every semantic/runtime choice before a live request can be made.

It performs no API call and does not mutate the source project.
"""

from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.prepass import d2l_b2_consistency_contract_v3_7 as b2_contract
from pipeline.prepass import d2l_b2_consolidation_contract_v1 as consolidation_contract
from pipeline.prepass import d2l_b2_multi_target_contract_v1 as multi_target_contract
from pipeline.prepass import d2l_candidate_discovery_v2 as discovery_contract
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    SOURCE_BINDING_SCHEMA,
    STAGE_IDS,
    canonical_sha256,
    file_sha256,
    validate_component_manifest,
    validate_source_binding,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    ComponentPlan,
    ComponentRunnerError,
)
from pipeline.translate import d2l_translation_slots_v1 as slot_contract
from pipeline.translate import runner as translation_runner_contract
from pipeline.translate import d2l_translation_quality_auditor_v3 as quality_contract
from pipeline.translate import d2l_translation_semantic_repair_v1 as repair_contract
from pipeline.translate.d2l_latex_markup_line_protected_spans_v5 import (
    POLICY_ID as S1_PROTECTED_POLICY_ID,
    PROMPT_VERSION as S1_TRANSLATION_PROMPT_VERSION,
)
from pipeline.translate.d2l_prompt_json_envelope_v2 import (
    POLICY_ID as TRANSLATOR_ENVELOPE_POLICY_ID,
)
from pipeline.translate.d2l_translation_slots_v1 import (
    POLICY_ID as TRANSLATION_OUTPUT_POLICY_ID,
    PROTECTED_LEXICAL_GLOSSARY_REVIEW_MATCH_RULE_ID,
    PROTECTED_LEXICAL_GLOSSARY_REVIEW_POLICY_ID,
    render_system_prompt as render_slot_system_prompt,
)


CATALOG_SCHEMA = "d2l_project_chapter_catalog_v2"
UNIVERSE_SCHEMA = "d2l_selected_universe_v2"
CONFIG_SCHEMA = "d2l_project_campaign_config_v2"
SEAL_SCHEMA = "d2l_project_campaign_seal_v2"
PREFLIGHT_SCHEMA = "d2l_project_campaign_preflight_v2"
TRANSPORT_SEAL_SCHEMA = "d2l_transport_attempt_seal_v2"
CAMPAIGN_VERSION = "d2l_project_campaign_runner_v2_5_transport_retry"
PIPELINE_ID = "d2l_terminology"
PIPELINE_VERSION = "d2l_translation_component_v1_4_fixed_only_hardened"
PROFILE_ID = "technical_d2l_v1"

SHOPAI_SOURCE_ID = "shopaikey_openai_proxy_v1"
MODELAPI_SOURCE_ID = "modelapi_shared_v1"
DEFAULT_HARD_TOTAL_TOKEN_CAP = 6_000_000
FORECAST_TOKEN_MULTIPLIER = 25
FORECAST_TOKEN_LOW_MULTIPLIER = 21
FORECAST_TOKEN_HIGH_MULTIPLIER = 31
TRANSPORT_POLICY_VERSION = "d2l_project_transport_v2"
TRANSPORT_RETRY_POLICY = {
    "max_retries": 2,
    "backoff_policy": "exponential",
    "initial_delay_ms": 1_000,
    "max_delay_ms": 4_000,
    "retryable_codes": [
        "connection",
        "rate_limit",
        "server_unavailable",
        "timeout",
    ],
}

ALLOWED_CHANNELS = (
    "semantic_text",
    "structured_translate",
    "preserve_only",
    "review_required",
)
B1_TARGET_TOKENS = 1_500
TRANSLATOR_TARGET_TOKENS = 1_100
TRANSLATOR_MAX_BLOCKS = 8
ORDERED_BLOCK_HASH_RULE = "d2l_ordered_block_ids_canonical_json_v1"
TOKEN_ESTIMATE_RULE = "utf8_text_char_count_ceil_div_4_v1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,190}$")
_SHA_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
_FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "credential",
    "password",
    "raw_prompt",
    "raw_response",
    "gold",
    "oracle",
    "reference_text",
    "human_reference",
    "score",
}
_RETIRED_SOURCE_IDS = {"local_gpt_gateway_v1"}


class D2LCampaignError(RuntimeError):
    """Raised when a campaign cannot be prepared or verified safely."""


@dataclass(frozen=True)
class LoadedProject:
    job_root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    package_root: Path
    document: dict[str, Any]
    projection: dict[str, Any]
    finalization_path: Path
    finalization: dict[str, Any]
    source_binding: dict[str, Any]
    chapter_rows: tuple[dict[str, Any], ...]
    block_rows: tuple[dict[str, Any], ...]
    source_snapshot: dict[str, Any]
    source_db_path: Path
    source_db_sha256: str


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_json_bytes(value))
    os.replace(temporary, path)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise D2LCampaignError(f"{label} is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise D2LCampaignError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise D2LCampaignError(f"{label} must be a JSON object")
    return value


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise D2LCampaignError(f"{label} must be a stable identifier")
    return value


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise D2LCampaignError(f"{label} must be a SHA-256")
    return value.upper()


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise D2LCampaignError(f"{label} must be a positive integer")
    return value


def resolve_code_revision(
    code_root: str | Path | None = None, *, require_clean: bool = True
) -> str:
    root = (
        Path(code_root).resolve()
        if code_root is not None
        else Path(__file__).resolve().parents[2]
    )
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise D2LCampaignError("cannot resolve the runtime Git revision") from exc
    revision = completed.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise D2LCampaignError("runtime Git revision is invalid")
    if require_clean:
        try:
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise D2LCampaignError("cannot verify the runtime Git state") from exc
        if status.stdout.strip():
            raise D2LCampaignError("runtime Git tree has tracked changes; commit before sealing")
    return revision


def _relative_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise D2LCampaignError(f"{label} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise D2LCampaignError(f"{label} must stay within its package")
    resolved = (root / relative).resolve()
    if root.resolve() not in resolved.parents:
        raise D2LCampaignError(f"{label} escapes its package")
    return resolved


def _reject_sensitive_keys(value: Any, label: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_KEYS or normalized.endswith("_secret"):
                raise D2LCampaignError(f"{label} contains forbidden key: {key}")
            _reject_sensitive_keys(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{label}[{index}]")


def _seal_body(body: Mapping[str, Any], *, count: int | None = None) -> dict[str, Any]:
    integrity: dict[str, Any] = {"payload_sha256": canonical_sha256(body)}
    if count is not None:
        integrity["row_count"] = count
    return {**deepcopy(dict(body)), "integrity": integrity}


def _verify_sealed_payload(
    value: Mapping[str, Any], *, schema: str, label: str
) -> dict[str, Any]:
    row = deepcopy(dict(value))
    if row.get("schema_version") != schema:
        raise D2LCampaignError(f"{label}.schema_version is invalid")
    integrity = row.pop("integrity", None)
    if not isinstance(integrity, Mapping):
        raise D2LCampaignError(f"{label}.integrity is missing")
    expected = _require_sha(integrity.get("payload_sha256"), f"{label}.integrity")
    if canonical_sha256(row) != expected:
        raise D2LCampaignError(f"{label} payload hash drift")
    return deepcopy(dict(value))


def _canonical_file_hash(path: Path, label: str) -> tuple[dict[str, Any], str]:
    value = _load_json(path, label)
    return value, canonical_sha256(value)


def _tree_rows(root: Path) -> list[dict[str, Any]]:
    paths: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise D2LCampaignError(f"source package contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise D2LCampaignError(f"source package contains an unsupported entry: {path}")
        paths.append(path)

    def inspect(path: Path) -> dict[str, Any]:
        return {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": file_sha256(path).lower(),
        }

    if len(paths) < 128:
        return [inspect(path) for path in paths]
    # Source packages contain many small assets. Parallel reads preserve the
    # already-sorted result while avoiding a multi-minute serial OneDrive scan.
    with ThreadPoolExecutor(max_workers=min(24, max(4, (os.cpu_count() or 4) * 2))) as pool:
        return list(pool.map(inspect, paths))


def _validate_source_manifest(path: Path) -> dict[str, Any]:
    manifest = _load_json(path, "source manifest")
    if manifest.get("contract_version") != "project_runtime_source_v2":
        raise D2LCampaignError("source manifest contract is unsupported")
    expected = _require_sha(
        manifest.get("manifest_payload_sha256"),
        "source_manifest.manifest_payload_sha256",
    )
    payload = dict(manifest)
    payload.pop("manifest_payload_sha256", None)
    if canonical_sha256(payload) != expected:
        raise D2LCampaignError("source manifest payload hash drift")
    for key in ("job_id", "project_id", "document_doc_id"):
        _require_id(manifest.get(key), f"source_manifest.{key}")
    return manifest


def _validate_document(
    document: Mapping[str, Any], manifest: Mapping[str, Any]
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    if document.get("schema_version") != "1.5.0":
        raise D2LCampaignError("document schema must remain 1.5.0")
    if document.get("doc_id") != manifest.get("document_doc_id"):
        raise D2LCampaignError("document doc_id does not match source manifest")
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise D2LCampaignError("document chapters must be a non-empty array")
    manifest_chapters = manifest.get("chapters")
    if not isinstance(manifest_chapters, list):
        raise D2LCampaignError("source manifest chapters must be an array")
    manifest_by_id = {
        str(row.get("chapter_id")): row
        for row in manifest_chapters
        if isinstance(row, Mapping)
    }
    chapter_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    seen_chapters: set[str] = set()
    seen_blocks: set[str] = set()
    global_order = 0
    for chapter_position, raw_chapter in enumerate(chapters):
        if not isinstance(raw_chapter, Mapping):
            raise D2LCampaignError("document chapter must be an object")
        chapter_id = _require_id(raw_chapter.get("chapter_id"), "chapter.chapter_id")
        if chapter_id in seen_chapters:
            raise D2LCampaignError(f"duplicate chapter_id: {chapter_id}")
        seen_chapters.add(chapter_id)
        if raw_chapter.get("order_index") != chapter_position:
            raise D2LCampaignError(f"chapter order is not contiguous: {chapter_id}")
        blocks = raw_chapter.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise D2LCampaignError(f"chapter has no blocks: {chapter_id}")
        manifest_row = manifest_by_id.get(chapter_id)
        if manifest_row is None:
            raise D2LCampaignError(f"chapter missing from source manifest: {chapter_id}")
        if manifest_row.get("block_count") != len(blocks):
            raise D2LCampaignError(f"chapter block count drift: {chapter_id}")
        chapter_rows.append(
            {
                "chapter_id": chapter_id,
                "source_position": chapter_position,
                "title": str(raw_chapter.get("title") or chapter_id),
                "block_count": len(blocks),
            }
        )
        for block_position, raw_block in enumerate(blocks):
            if not isinstance(raw_block, Mapping):
                raise D2LCampaignError(f"block in {chapter_id} must be an object")
            block_id = _require_id(raw_block.get("block_id"), "block.block_id")
            if block_id in seen_blocks:
                raise D2LCampaignError(f"duplicate block_id: {block_id}")
            seen_blocks.add(block_id)
            if raw_block.get("order_index") != global_order:
                raise D2LCampaignError(f"global block order is not contiguous: {block_id}")
            block_type = raw_block.get("block_type")
            source_text = raw_block.get("source_text")
            clean_text = raw_block.get("clean_text")
            if not isinstance(block_type, str) or not block_type:
                raise D2LCampaignError(f"block_type is invalid: {block_id}")
            if not isinstance(source_text, str) or not isinstance(clean_text, str):
                raise D2LCampaignError(f"block text is invalid: {block_id}")
            block_rows.append(
                {
                    "chapter_id": chapter_id,
                    "chapter_position": chapter_position,
                    "block_id": block_id,
                    "block_position": block_position,
                    "document_order_index": global_order,
                    "block_type": block_type,
                    "source_text": source_text,
                    "clean_text": clean_text,
                }
            )
            global_order += 1
    if len(chapter_rows) != manifest.get("chapter_count"):
        raise D2LCampaignError("source manifest chapter_count drift")
    if len(block_rows) != manifest.get("block_count"):
        raise D2LCampaignError("source manifest block_count drift")
    if [row["chapter_id"] for row in chapter_rows] != [
        str(row.get("chapter_id")) for row in manifest_chapters
    ]:
        raise D2LCampaignError("source manifest chapter order drift")
    return tuple(chapter_rows), tuple(block_rows)


def _validate_projection(
    projection: Mapping[str, Any],
    *,
    document_sha256: str,
    structure_sha256: str,
    asset_sha256: str,
    block_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    if projection.get("schema_version") != "admitted_projection_v1":
        raise D2LCampaignError("admitted projection schema is invalid")
    integrity = projection.get("integrity")
    if not isinstance(integrity, Mapping):
        raise D2LCampaignError("admitted projection integrity is missing")
    expected_payload = _require_sha(
        integrity.get("payload_sha256"), "admitted_projection.integrity.payload_sha256"
    )
    payload = dict(projection)
    payload.pop("integrity", None)
    if canonical_sha256(payload) != expected_payload:
        raise D2LCampaignError("admitted projection payload hash drift")
    rows = projection.get("rows")
    if not isinstance(rows, list) or len(rows) != len(block_rows):
        raise D2LCampaignError("admitted projection does not exact-cover document")
    if integrity.get("row_count") != len(rows):
        raise D2LCampaignError("admitted projection row_count drift")
    inputs = projection.get("inputs")
    expected_inputs = {
        "document": document_sha256,
        "structure": structure_sha256,
        "asset_manifest": asset_sha256,
    }
    if not isinstance(inputs, Mapping):
        raise D2LCampaignError("admitted projection inputs are missing")
    for key, digest in expected_inputs.items():
        raw = inputs.get(key)
        if not isinstance(raw, Mapping) or str(raw.get("sha256", "")).upper() != digest:
            raise D2LCampaignError(f"admitted projection {key} hash drift")
    result: list[dict[str, str]] = []
    for index, (raw, block) in enumerate(zip(rows, block_rows, strict=True)):
        if not isinstance(raw, Mapping):
            raise D2LCampaignError(f"admitted projection row {index} is invalid")
        chapter_id = str(raw.get("chapter_id") or "")
        block_id = str(raw.get("block_id") or "")
        channel = str(raw.get("channel") or "")
        if chapter_id != block["chapter_id"] or block_id != block["block_id"]:
            raise D2LCampaignError(f"admitted projection order drift at row {index}")
        if channel not in ALLOWED_CHANNELS:
            raise D2LCampaignError(f"unsupported admission channel: {channel}")
        result.append(
            {"chapter_id": chapter_id, "block_id": block_id, "channel": channel}
        )
    return tuple(result)


def _typed_binding(
    *,
    artifact_ref: str,
    artifact_kind: str,
    schema_version: str,
    path: Path,
) -> dict[str, str]:
    return {
        "artifact_ref": _require_id(artifact_ref, "artifact_ref"),
        "artifact_kind": artifact_kind,
        "schema_version": schema_version,
        "sha256": file_sha256(path),
        "sha256_kind": "physical",
    }


def load_project(job_root: str | Path, *, verify_tree: bool = True) -> LoadedProject:
    root = Path(job_root).resolve()
    if not root.is_dir():
        raise D2LCampaignError(f"job root does not exist: {root}")
    manifest_path = root / "source_manifest.json"
    manifest = _validate_source_manifest(manifest_path)
    snapshot = manifest.get("source_package_snapshot")
    if not isinstance(snapshot, Mapping):
        raise D2LCampaignError("source package snapshot is missing")
    package_root = _relative_path(root, snapshot.get("path"), "source_package_snapshot.path")
    if not package_root.is_dir():
        raise D2LCampaignError("source package snapshot directory is missing")
    expected_rows = snapshot.get("rows")
    if not isinstance(expected_rows, list):
        raise D2LCampaignError("source package tree rows are missing")
    if snapshot.get("file_count") != len(expected_rows):
        raise D2LCampaignError("source package file_count drift")
    if canonical_sha256(expected_rows) != _require_sha(
        snapshot.get("tree_sha256"), "source_package_snapshot.tree_sha256"
    ):
        raise D2LCampaignError("source package declared tree hash drift")
    if verify_tree:
        observed_rows = _tree_rows(package_root)
        if observed_rows != expected_rows:
            raise D2LCampaignError("source package file tree drift")

    package_files = {
        "document": ("document.json", "source_document"),
        "structure_manifest": ("structure_manifest.json", "structure_manifest"),
        "asset_manifest": ("asset_manifest.json", "asset_manifest"),
        "admitted_projection": ("admitted_projection_v1.json", "admitted_projection"),
        "normalization_receipt": ("normalization_receipt.json", "normalization_receipt"),
    }
    loaded: dict[str, tuple[Path, dict[str, Any], str]] = {}
    for key, (filename, _kind) in package_files.items():
        path = package_root / filename
        value, logical_hash = _canonical_file_hash(path, key)
        loaded[key] = (path, value, logical_hash)

    finalization_snapshot = manifest.get("finalization_snapshot")
    if not isinstance(finalization_snapshot, Mapping):
        raise D2LCampaignError("finalization snapshot is missing")
    finalization_path = _relative_path(
        root, finalization_snapshot.get("path"), "finalization_snapshot.path"
    )
    if file_sha256(finalization_path) != _require_sha(
        finalization_snapshot.get("sha256"), "finalization_snapshot.sha256"
    ):
        raise D2LCampaignError("finalization file hash drift")
    finalization = _load_json(finalization_path, "source finalization")
    if finalization.get("schema_version") != "source_package_finalization_v1":
        raise D2LCampaignError("source finalization schema is invalid")
    finalization_integrity = finalization.get("integrity")
    if not isinstance(finalization_integrity, Mapping):
        raise D2LCampaignError("source finalization integrity is missing")
    finalization_payload = dict(finalization)
    finalization_payload.pop("integrity", None)
    finalization_payload_sha = canonical_sha256(finalization_payload)
    if finalization_payload_sha != _require_sha(
        finalization_integrity.get("payload_sha256"),
        "source_finalization.integrity.payload_sha256",
    ):
        raise D2LCampaignError("source finalization payload hash drift")
    if finalization_payload_sha != _require_sha(
        finalization_snapshot.get("payload_sha256"),
        "finalization_snapshot.payload_sha256",
    ):
        raise D2LCampaignError("source finalization manifest binding drift")
    if finalization.get("doc_id") != manifest.get("document_doc_id"):
        raise D2LCampaignError("source finalization doc_id drift")
    if not str(finalization.get("lifecycle") or "").startswith("finalized"):
        raise D2LCampaignError("source package is not finalized")
    final_package = finalization.get("package")
    if not isinstance(final_package, Mapping):
        raise D2LCampaignError("source finalization package bindings are missing")
    logical_keys = {
        "document": "document",
        "structure_manifest": "structure",
        "asset_manifest": "asset_manifest",
        "admitted_projection": "admitted_projection",
        "normalization_receipt": "normalization_receipt",
    }
    for loaded_key, final_key in logical_keys.items():
        final_row = final_package.get(final_key)
        if not isinstance(final_row, Mapping):
            raise D2LCampaignError(f"source finalization lacks {final_key}")
        if str(final_row.get("sha256", "")).upper() != loaded[loaded_key][2]:
            raise D2LCampaignError(f"source finalization {final_key} hash drift")

    document = loaded["document"][1]
    chapter_rows, block_rows = _validate_document(document, manifest)
    projection = loaded["admitted_projection"][1]
    projection_rows = _validate_projection(
        projection,
        document_sha256=loaded["document"][2],
        structure_sha256=loaded["structure_manifest"][2],
        asset_sha256=loaded["asset_manifest"][2],
        block_rows=block_rows,
    )
    joined_blocks = tuple(
        {**dict(block), "channel": admission["channel"]}
        for block, admission in zip(block_rows, projection_rows, strict=True)
    )

    translatable = manifest.get("translatable_chapter_ids")
    if not isinstance(translatable, list) or len(set(translatable)) != len(translatable):
        raise D2LCampaignError("translatable_chapter_ids is invalid")
    source_chapter_ids = [row["chapter_id"] for row in chapter_rows]
    if [item for item in source_chapter_ids if item in set(translatable)] != translatable:
        raise D2LCampaignError("translatable_chapter_ids is not in source order")

    tree_sha = _require_sha(snapshot.get("tree_sha256"), "source package tree sha")
    prefix = tree_sha[:16].lower()
    source_binding = {
        "schema": SOURCE_BINDING_SCHEMA,
        "document": _typed_binding(
            artifact_ref=f"srcpkg_{prefix}_document",
            artifact_kind="source_document",
            schema_version=str(loaded["document"][1].get("schema_version")),
            path=loaded["document"][0],
        ),
        "structure_manifest": _typed_binding(
            artifact_ref=f"srcpkg_{prefix}_structure",
            artifact_kind="structure_manifest",
            schema_version=str(loaded["structure_manifest"][1].get("schema_version")),
            path=loaded["structure_manifest"][0],
        ),
        "asset_manifest": _typed_binding(
            artifact_ref=f"srcpkg_{prefix}_assets",
            artifact_kind="asset_manifest",
            schema_version=str(loaded["asset_manifest"][1].get("schema_version")),
            path=loaded["asset_manifest"][0],
        ),
        "admitted_projection": _typed_binding(
            artifact_ref=f"srcpkg_{prefix}_projection",
            artifact_kind="admitted_projection",
            schema_version=str(loaded["admitted_projection"][1].get("schema_version")),
            path=loaded["admitted_projection"][0],
        ),
        "normalization_receipt": _typed_binding(
            artifact_ref=f"srcpkg_{prefix}_receipt",
            artifact_kind="normalization_receipt",
            schema_version=str(loaded["normalization_receipt"][1].get("schema_version")),
            path=loaded["normalization_receipt"][0],
        ),
        "package_seal": _typed_binding(
            artifact_ref=f"srcpkg_{prefix}_seal",
            artifact_kind="source_package_seal",
            schema_version=str(finalization.get("schema_version")),
            path=finalization_path,
        ),
    }
    validate_source_binding(source_binding)
    source_db_path = root / "memory.sqlite3"
    source_db_sha = file_sha256(source_db_path)
    if source_db_sha != _require_sha(
        manifest.get("initial_runtime_db_sha256"),
        "source_manifest.initial_runtime_db_sha256",
    ):
        raise D2LCampaignError("source runtime database hash drift")
    return LoadedProject(
        job_root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        package_root=package_root,
        document=document,
        projection=projection,
        finalization_path=finalization_path,
        finalization=finalization,
        source_binding=source_binding,
        chapter_rows=chapter_rows,
        block_rows=joined_blocks,
        source_snapshot={
            "source_manifest_sha256": file_sha256(manifest_path),
            "source_manifest_payload_sha256": _require_sha(
                manifest["manifest_payload_sha256"], "manifest payload"
            ),
            "package_tree_sha256": tree_sha,
            "package_file_count": len(expected_rows),
            "source_document_sha256": _require_sha(
                finalization.get("source", {}).get("sha256"), "source document sha"
            ),
            "document_logical_sha256": loaded["document"][2],
            "source_db_sha256": source_db_sha,
            "finalization_payload_sha256": finalization_payload_sha,
        },
        source_db_path=source_db_path,
        source_db_sha256=source_db_sha,
    )


def select_chapters(
    project: LoadedProject,
    *,
    chapter_ids: Sequence[str] | None = None,
    start_chapter: str | None = None,
    end_chapter: str | None = None,
    all_chapters: bool = False,
) -> tuple[str, tuple[str, ...]]:
    explicit = tuple(chapter_ids or ())
    mode_count = int(bool(explicit)) + int(start_chapter is not None or end_chapter is not None) + int(all_chapters)
    if mode_count != 1:
        raise D2LCampaignError(
            "choose exactly one selection mode: chapters, inclusive range, or all"
        )
    available = [
        row["chapter_id"]
        for row in project.chapter_rows
        if row["chapter_id"] in set(project.manifest["translatable_chapter_ids"])
    ]
    positions = {chapter_id: index for index, chapter_id in enumerate(available)}
    if all_chapters:
        return "all", tuple(available)
    if explicit:
        if len(set(explicit)) != len(explicit):
            raise D2LCampaignError("explicit chapter selection contains duplicates")
        unknown = [chapter_id for chapter_id in explicit if chapter_id not in positions]
        if unknown:
            raise D2LCampaignError(f"unknown or non-translatable chapters: {unknown}")
        if [positions[item] for item in explicit] != sorted(positions[item] for item in explicit):
            raise D2LCampaignError("explicit chapters must follow source order")
        return "explicit", explicit
    if start_chapter is None or end_chapter is None:
        raise D2LCampaignError("range selection requires both start and end chapter")
    if start_chapter not in positions or end_chapter not in positions:
        raise D2LCampaignError("range endpoint is unknown or non-translatable")
    start = positions[start_chapter]
    end = positions[end_chapter]
    if start > end:
        raise D2LCampaignError("chapter range is reversed")
    return "range", tuple(available[start : end + 1])


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _window_summary(
    blocks: Sequence[Mapping[str, Any]],
    *,
    target_tokens: int,
    max_blocks: int | None,
) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    by_chapter: dict[str, list[Mapping[str, Any]]] = {}
    for block in blocks:
        by_chapter.setdefault(str(block["chapter_id"]), []).append(block)
    for chapter_id, chapter_blocks in by_chapter.items():
        current: list[str] = []
        current_tokens = 0
        counter = 0
        for block in chapter_blocks:
            estimate = _estimate_tokens(str(block.get("clean_text") or block.get("source_text") or ""))
            if estimate > target_tokens:
                if current:
                    counter += 1
                    windows.append(
                        {
                            "window_id": f"w_{chapter_id}_{counter:04d}",
                            "chapter_id": chapter_id,
                            "block_ids": current,
                            "estimated_source_tokens": current_tokens,
                        }
                    )
                    current = []
                    current_tokens = 0
                counter += 1
                windows.append(
                    {
                        "window_id": f"w_{chapter_id}_{counter:04d}",
                        "chapter_id": chapter_id,
                        "block_ids": [str(block["block_id"])],
                        "estimated_source_tokens": estimate,
                    }
                )
                continue
            over_tokens = bool(current) and current_tokens + estimate > target_tokens
            over_blocks = max_blocks is not None and len(current) >= max_blocks
            if current and (over_tokens or over_blocks):
                counter += 1
                windows.append(
                    {
                        "window_id": f"w_{chapter_id}_{counter:04d}",
                        "chapter_id": chapter_id,
                        "block_ids": current,
                        "estimated_source_tokens": current_tokens,
                    }
                )
                current = []
                current_tokens = 0
            current.append(str(block["block_id"]))
            current_tokens += estimate
        if current:
            counter += 1
            windows.append(
                {
                    "window_id": f"w_{chapter_id}_{counter:04d}",
                    "chapter_id": chapter_id,
                    "block_ids": current,
                    "estimated_source_tokens": current_tokens,
                }
            )
    return {
        "target_source_tokens": target_tokens,
        "max_blocks": max_blocks,
        "cross_chapter": False,
        "token_estimate_rule": TOKEN_ESTIMATE_RULE,
        "window_count": len(windows),
        "estimated_source_tokens": sum(row["estimated_source_tokens"] for row in windows),
        "windows_sha256": canonical_sha256(windows),
        "windows": windows,
    }


def build_chapter_catalog(project: LoadedProject) -> dict[str, Any]:
    channel_counts: dict[str, dict[str, int]] = {
        row["chapter_id"]: {channel: 0 for channel in ALLOWED_CHANNELS}
        for row in project.chapter_rows
    }
    for block in project.block_rows:
        channel_counts[block["chapter_id"]][block["channel"]] += 1
    translatable = set(project.manifest["translatable_chapter_ids"])
    rows = [
        {
            **dict(chapter),
            "translatable": chapter["chapter_id"] in translatable,
            "channel_counts": channel_counts[chapter["chapter_id"]],
        }
        for chapter in project.chapter_rows
    ]
    body = {
        "schema_version": CATALOG_SCHEMA,
        "project_id": project.manifest["project_id"],
        "job_id": project.manifest["job_id"],
        "doc_id": project.manifest["document_doc_id"],
        "source_binding_sha256": canonical_sha256(project.source_binding),
        "chapter_count": len(rows),
        "block_count": len(project.block_rows),
        "chapters": rows,
    }
    return _seal_body(body, count=len(rows))


def build_selected_universe(
    project: LoadedProject,
    *,
    selection_mode: str,
    selected_chapter_ids: Sequence[str],
) -> dict[str, Any]:
    selected_set = set(selected_chapter_ids)
    selected = [row for row in project.block_rows if row["chapter_id"] in selected_set]
    compact_blocks = [
        {
            "chapter_id": row["chapter_id"],
            "chapter_position": row["chapter_position"],
            "block_id": row["block_id"],
            "block_position": row["block_position"],
            "document_order_index": row["document_order_index"],
            "block_type": row["block_type"],
            "channel": row["channel"],
            "source_text_sha256": canonical_sha256(row["source_text"]),
            "clean_text_sha256": canonical_sha256(row["clean_text"]),
            "source_char_count": len(row["source_text"]),
            "clean_char_count": len(row["clean_text"]),
            "estimated_source_tokens": _estimate_tokens(
                str(row["clean_text"] or row["source_text"])
            ),
        }
        for row in selected
    ]
    counts = {channel: 0 for channel in ALLOWED_CHANNELS}
    for row in selected:
        counts[row["channel"]] += 1
    chapter_lookup = {row["chapter_id"]: row for row in project.chapter_rows}
    chapter_rows = []
    for chapter_id in selected_chapter_ids:
        chapter_blocks = [row for row in selected if row["chapter_id"] == chapter_id]
        channel_counts = {channel: 0 for channel in ALLOWED_CHANNELS}
        for row in chapter_blocks:
            channel_counts[row["channel"]] += 1
        chapter_rows.append(
            {
                **dict(chapter_lookup[chapter_id]),
                "channel_counts": channel_counts,
            }
        )
    b1_blocks = [row for row in selected if row["channel"] == "semantic_text"]
    translator_blocks = [
        row
        for row in selected
        if row["channel"] in {"semantic_text", "structured_translate"}
    ]
    body = {
        "schema_version": UNIVERSE_SCHEMA,
        "project_id": project.manifest["project_id"],
        "job_id": project.manifest["job_id"],
        "doc_id": project.manifest["document_doc_id"],
        "source_binding_sha256": canonical_sha256(project.source_binding),
        "selection": {
            "mode": selection_mode,
            "selected_chapter_ids": list(selected_chapter_ids),
        },
        "chapters": chapter_rows,
        "ordered_block_ids_sha256": canonical_sha256(
            [row["block_id"] for row in selected]
        ),
        "ordered_block_ids_hash_rule": ORDERED_BLOCK_HASH_RULE,
        "block_count": len(selected),
        "channel_counts": counts,
        "routing": {
            "b1_b2_channels": ["semantic_text"],
            "b1_b2_block_count": len(b1_blocks),
            "translator_channels": ["semantic_text", "structured_translate"],
            "translator_llm_block_count": len(translator_blocks),
            "preserve_only_block_count": counts["preserve_only"],
            "review_held_block_count": counts["review_required"],
            "review_required_sent_to_llm": False,
        },
        "window_estimates": {
            "b1": _window_summary(
                b1_blocks, target_tokens=B1_TARGET_TOKENS, max_blocks=None
            ),
            "translator": _window_summary(
                translator_blocks,
                target_tokens=TRANSLATOR_TARGET_TOKENS,
                max_blocks=TRANSLATOR_MAX_BLOCKS,
            ),
        },
        "blocks": compact_blocks,
    }
    return _seal_body(body, count=len(compact_blocks))


def _output_contract(validator_id: str) -> dict[str, Any]:
    return {
        "structured_output_mode": "disabled",
        "envelope": "prompt_generated_json",
        "native_schema_parameter_sent": False,
        "canonical_local_validator_id": validator_id,
        "local_validator_required": True,
    }


def _role(
    *,
    role_id: str,
    stage_id: str,
    model_id: str,
    source_id: str,
    prompt_id: str,
    prompt_sha256: str,
    validator_id: str,
    validator_sha256: str,
    response_schema_sha256: str,
    max_input_tokens: int,
    max_output_tokens: int,
    temperature: float,
    seed: int | None,
    reasoning_effort: str,
    verbosity: str,
    semantic_retry_cap: int,
    extra_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body = {
        "role_id": role_id,
        "stage_id": stage_id,
        "model_id": model_id,
        "source_id": source_id,
        "prompt": {"id": prompt_id, "sha256": _require_sha(prompt_sha256, "prompt sha")},
        "response_schema_sha256": _require_sha(
            response_schema_sha256, "response schema sha"
        ),
        "validator_id": validator_id,
        "validator_sha256": _require_sha(
            validator_sha256, "validator implementation sha"
        ),
        "generation": {
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
            "top_p": 1.0,
            "seed": seed,
            "reasoning_effort": reasoning_effort,
            "verbosity": verbosity,
        },
        "output_contract": _output_contract(validator_id),
        "semantic_retry_cap": semantic_retry_cap,
        "extra_policy": dict(extra_policy or {}),
    }
    return {**body, "semantic_role_sha256": canonical_sha256(body)}


def semantic_role_profiles() -> list[dict[str, Any]]:
    s0_prompt = S1_TRANSLATION_PROMPT_VERSION
    s0_system = render_slot_system_prompt(s0_prompt)
    s1_system = render_slot_system_prompt(S1_TRANSLATION_PROMPT_VERSION)
    candidate_schema_hash = canonical_sha256(discovery_contract.RESPONSE_SCHEMA)
    b2_schema_hash = canonical_sha256(b2_contract.RESPONSE_FORMAT)
    return [
        _role(
            role_id="d2l.candidate_discovery",
            stage_id="b1_candidate_discovery",
            model_id="gemini-3.5-flash",
            source_id=SHOPAI_SOURCE_ID,
            prompt_id=discovery_contract.PROMPT_VERSION,
            prompt_sha256=discovery_contract.prompt_sha256(),
            validator_id=discovery_contract.VALIDATOR_VERSION,
            validator_sha256=file_sha256(Path(discovery_contract.__file__)),
            response_schema_sha256=candidate_schema_hash,
            max_input_tokens=6_000,
            max_output_tokens=6_144,
            temperature=1.0,
            seed=20_260_612,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=1,
            extra_policy={"window_source_token_target": B1_TARGET_TOKENS, "max_blocks": None},
        ),
        _role(
            role_id="d2l.b2.admission",
            stage_id="b2_admission_translation",
            model_id="gpt-5.4",
            source_id=MODELAPI_SOURCE_ID,
            prompt_id=b2_contract.PROMPT_VERSION,
            prompt_sha256=b2_contract.prompt_sha256(),
            validator_id=b2_contract.VALIDATOR_VERSION,
            validator_sha256=file_sha256(Path(b2_contract.__file__)),
            response_schema_sha256=b2_schema_hash,
            max_input_tokens=6_250,
            max_output_tokens=4_096,
            temperature=1.0,
            seed=20_260_718,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=0,
        ),
        _role(
            role_id="d2l.b2.morphology",
            stage_id="auditor_morphology",
            model_id="gpt-5.5",
            source_id=MODELAPI_SOURCE_ID,
            prompt_id=consolidation_contract.PROMPT_VERSION,
            prompt_sha256=consolidation_contract.prompt_sha256(),
            validator_id=consolidation_contract.VALIDATOR_VERSION,
            validator_sha256=file_sha256(Path(consolidation_contract.__file__)),
            response_schema_sha256=consolidation_contract.response_schema_sha256(),
            max_input_tokens=6_000,
            max_output_tokens=4_096,
            temperature=1.0,
            seed=20_260_719,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=0,
        ),
        _role(
            role_id="d2l.b2.target_collision",
            stage_id="auditor_target_collision",
            model_id="gpt-5.5",
            source_id=MODELAPI_SOURCE_ID,
            prompt_id=consolidation_contract.PROMPT_VERSION,
            prompt_sha256=consolidation_contract.prompt_sha256(),
            validator_id=consolidation_contract.VALIDATOR_VERSION,
            validator_sha256=file_sha256(Path(consolidation_contract.__file__)),
            response_schema_sha256=consolidation_contract.response_schema_sha256(),
            max_input_tokens=6_000,
            max_output_tokens=4_096,
            temperature=1.0,
            seed=20_260_719,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=0,
        ),
        _role(
            role_id="d2l.b2.multi_target",
            stage_id="auditor_multi_target",
            model_id="gpt-5.5",
            source_id=MODELAPI_SOURCE_ID,
            prompt_id=multi_target_contract.PROMPT_VERSION,
            prompt_sha256=multi_target_contract.prompt_sha256(),
            validator_id=multi_target_contract.VALIDATOR_VERSION,
            validator_sha256=file_sha256(Path(multi_target_contract.__file__)),
            response_schema_sha256=multi_target_contract.response_schema_sha256(),
            max_input_tokens=6_250,
            max_output_tokens=4_096,
            temperature=1.0,
            seed=20_260_718,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=0,
        ),
        _role(
            role_id="d2l.translator.s0",
            stage_id="translator",
            model_id="gpt-5.4",
            source_id=MODELAPI_SOURCE_ID,
            prompt_id=s0_prompt,
            prompt_sha256=canonical_sha256(s0_system),
            validator_id="d2l_translation_slots_v4_local_validator",
            validator_sha256=file_sha256(Path(slot_contract.__file__)),
            response_schema_sha256=canonical_sha256(
                {"type": "object", "required": ["translations"]}
            ),
            max_input_tokens=8_192,
            max_output_tokens=4_096,
            temperature=0.3,
            seed=20_260_612,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=1,
            extra_policy={
                "arm_id": "s0",
                "protected_spans_policy": S1_PROTECTED_POLICY_ID,
                "translation_output_policy": TRANSLATION_OUTPUT_POLICY_ID,
                "response_envelope_policy": TRANSLATOR_ENVELOPE_POLICY_ID,
                "glossary_visibility": "none",
                "mechanical_retry_cap_per_window": 1,
                "window_target_tokens": TRANSLATOR_TARGET_TOKENS,
                "max_blocks": TRANSLATOR_MAX_BLOCKS,
            },
        ),
        _role(
            role_id="d2l.translator.s1",
            stage_id="translator",
            model_id="gpt-5.4",
            source_id=MODELAPI_SOURCE_ID,
            prompt_id=S1_TRANSLATION_PROMPT_VERSION,
            prompt_sha256=canonical_sha256(s1_system),
            validator_id="d2l_translation_slots_v4_local_validator",
            validator_sha256=file_sha256(Path(slot_contract.__file__)),
            response_schema_sha256=canonical_sha256(
                {"type": "object", "required": ["translations"]}
            ),
            max_input_tokens=8_192,
            max_output_tokens=4_096,
            temperature=0.3,
            seed=20_260_612,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=1,
            extra_policy={
                "arm_id": "s1",
                "protected_spans_policy": S1_PROTECTED_POLICY_ID,
                "translation_output_policy": TRANSLATION_OUTPUT_POLICY_ID,
                "response_envelope_policy": TRANSLATOR_ENVELOPE_POLICY_ID,
                "glossary_review_policy": PROTECTED_LEXICAL_GLOSSARY_REVIEW_POLICY_ID,
                "glossary_review_match_rule": PROTECTED_LEXICAL_GLOSSARY_REVIEW_MATCH_RULE_ID,
                "mechanical_retry_cap_per_window": 1,
                "window_target_tokens": TRANSLATOR_TARGET_TOKENS,
                "max_blocks": TRANSLATOR_MAX_BLOCKS,
            },
        ),
        _role(
            role_id="d2l.translator.s0.semantic_repair",
            stage_id="translation_quality_audit",
            model_id="gpt-5.4",
            source_id=MODELAPI_SOURCE_ID,
            prompt_id=repair_contract.PROMPT_ID,
            prompt_sha256=repair_contract.prompt_sha256(),
            validator_id=repair_contract.LOCAL_VALIDATOR_ID,
            validator_sha256=file_sha256(Path(repair_contract.__file__)),
            response_schema_sha256=repair_contract.response_schema_sha256(),
            max_input_tokens=8_192,
            max_output_tokens=4_096,
            temperature=0.3,
            seed=20_260_612,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=0,
            extra_policy={
                "arm_id": "s0",
                "semantic_repair_cap_per_window": 1,
                "full_original_window_context": True,
                "original_context_pack": "absent_by_design",
                "output_scope": "major_blocks_only",
                "post_repair_validation": "deterministic_only_no_second_auditor",
            },
        ),
        _role(
            role_id="d2l.translator.s1.semantic_repair",
            stage_id="translation_quality_audit",
            model_id="gpt-5.4",
            source_id=MODELAPI_SOURCE_ID,
            prompt_id=repair_contract.PROMPT_ID,
            prompt_sha256=repair_contract.prompt_sha256(),
            validator_id=repair_contract.LOCAL_VALIDATOR_ID,
            validator_sha256=file_sha256(Path(repair_contract.__file__)),
            response_schema_sha256=repair_contract.response_schema_sha256(),
            max_input_tokens=8_192,
            max_output_tokens=4_096,
            temperature=0.3,
            seed=20_260_612,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=0,
            extra_policy={
                "arm_id": "s1",
                "semantic_repair_cap_per_window": 1,
                "full_original_window_context": True,
                "original_context_pack": "exact_persisted_initial_pack",
                "output_scope": "major_blocks_only",
                "post_repair_validation": "deterministic_only_no_second_auditor",
            },
        ),
        _role(
            role_id="d2l.translator.quality_auditor",
            stage_id="translation_quality_audit",
            model_id="gemini-3.5-flash",
            source_id=SHOPAI_SOURCE_ID,
            prompt_id=quality_contract.PROMPT_ID,
            prompt_sha256=quality_contract.prompt_sha256(),
            validator_id=quality_contract.LOCAL_VALIDATOR_ID,
            validator_sha256=file_sha256(Path(quality_contract.__file__)),
            response_schema_sha256=quality_contract.response_schema_sha256(),
            max_input_tokens=8_192,
            max_output_tokens=2_048,
            temperature=0.0,
            seed=None,
            reasoning_effort="none",
            verbosity="low",
            semantic_retry_cap=1,
            extra_policy={
                "input_contract": quality_contract.INPUT_CONTRACT_VERSION,
                "semantic_contract": quality_contract.SEMANTIC_CONTRACT_VERSION,
                "authority": "findings_only_non_blocking",
                "glossary_visibility": "none",
                "major_finding_repair_policy": (
                    "one_independent_translator_semantic_repair"
                ),
                "post_repair_validation": "deterministic_only_no_second_auditor",
            },
        ),
    ]


def initial_transport_sources() -> dict[str, dict[str, Any]]:
    shop = {
        "source_id": SHOPAI_SOURCE_ID,
        "source_revision": "chat_completions_prompt_json_v1",
        # Shared Backend classifies the transport topology here. Third-party
        # trust is expressed by output_mode/native_schema_parameter_sent.
        "source_class": "remote_api",
        "endpoint_class": "remote",
        "base_url": "https://api.shopaikey.com/v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "credential_ref": "credential.shopaikey_gemini_proxy_v1",
        "credential_family": "SHOPAIKEY_GEMINI_PROXY",
        "physical_quota_bucket_id": "shopaikey-gemini-d2l-v1.gemini-3.5-flash",
        "supported_model_ids": ["gemini-3.5-flash"],
        "output_mode": "prompt_generated_json",
        "native_schema_parameter_sent": False,
    }
    modelapi = {
        "source_id": MODELAPI_SOURCE_ID,
        "source_revision": "modelapi_profile_v1",
        "source_class": "remote_api",
        "endpoint_class": "remote",
        "base_url": "https://modelapi.vn/v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "credential_ref": "credential.modelapi_shared_v1",
        "credential_family": "MODELAPI_SHARED",
        "physical_quota_bucket_id": "modelapi-shared-v1",
        "supported_model_ids": ["gpt-5.4", "gpt-5.5"],
        "output_mode": "prompt_generated_json",
        "native_schema_parameter_sent": False,
    }
    return {shop["source_id"]: shop, modelapi["source_id"]: modelapi}


def _build_limits(universe: Mapping[str, Any], roles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    routing = universe["routing"]
    windows = universe["window_estimates"]
    b1_windows = int(windows["b1"]["window_count"])
    translator_windows = int(windows["translator"]["window_count"])
    semantic_blocks = int(routing["b1_b2_block_count"])
    b2_packets = max(1, (semantic_blocks + 3) // 4)
    component_caps = max(1, (b2_packets + 3) // 4)
    semantic_request_caps = {
        "d2l.candidate_discovery": b1_windows,
        "d2l.b2.admission": b2_packets,
        "d2l.b2.morphology": component_caps,
        "d2l.b2.target_collision": component_caps,
        "d2l.b2.multi_target": component_caps,
        "d2l.translator.s0": translator_windows,
        "d2l.translator.s1": translator_windows,
        "d2l.translator.s0.semantic_repair": translator_windows,
        "d2l.translator.s1.semantic_repair": translator_windows,
        "d2l.translator.quality_auditor": translator_windows * 2,
    }
    role_limits = []
    total_attempts = 0
    total_tokens = 0
    transport_retry_cap = int(TRANSPORT_RETRY_POLICY["max_retries"])
    physical_attempts_per_semantic_attempt = 1 + transport_retry_cap
    for role in roles:
        request_cap = semantic_request_caps[role["role_id"]]
        semantic_attempts_per_request = 1 + int(role["semantic_retry_cap"])
        attempt_cap = (
            request_cap
            * semantic_attempts_per_request
            * physical_attempts_per_semantic_attempt
        )
        per_call = int(role["generation"]["max_input_tokens"]) + int(
            role["generation"]["max_output_tokens"]
        )
        role_token_cap = attempt_cap * per_call
        total_attempts += attempt_cap
        total_tokens += role_token_cap
        role_limits.append(
            {
                "role_id": role["role_id"],
                "semantic_request_cap": request_cap,
                "semantic_attempts_per_request": semantic_attempts_per_request,
                "transport_retry_cap": transport_retry_cap,
                "physical_attempts_per_semantic_attempt": (
                    physical_attempts_per_semantic_attempt
                ),
                "physical_attempt_cap": attempt_cap,
                "max_input_tokens_per_attempt": role["generation"]["max_input_tokens"],
                "max_output_tokens_per_attempt": role["generation"]["max_output_tokens"],
                "max_total_tokens": role_token_cap,
            }
        )
    source_tokens = sum(int(row["estimated_source_tokens"]) for row in universe["blocks"])
    forecast_tokens = source_tokens * FORECAST_TOKEN_MULTIPLIER
    return {
        "cap_semantics": "hard_stop_ceiling_not_expected_usage",
        "derivation": "selected_universe_conservative_v3_transport_retry",
        "roles": role_limits,
        "hard_physical_attempt_cap": total_attempts,
        "theoretical_role_reserve_tokens": total_tokens,
        "hard_total_token_cap": min(total_tokens, DEFAULT_HARD_TOTAL_TOKEN_CAP),
        "reserved_cost_cap_usd": None,
        "forecast_cost_usd": None,
        "forecast_total_tokens": forecast_tokens,
        "forecast_token_range": {
            "low": source_tokens * FORECAST_TOKEN_LOW_MULTIPLIER,
            "high": source_tokens * FORECAST_TOKEN_HIGH_MULTIPLIER,
        },
        "forecast_status": "empirical_token_range_no_authoritative_tariff",
        "cost_basis": "token_forecast_only_usd_unknown_without_pinned_tariff",
    }


def build_campaign_config(
    project: LoadedProject,
    universe: Mapping[str, Any],
    *,
    workflow_run_id: str,
    component_run_id: str,
    code_revision: str,
    reserved_cost_cap_usd: str | Decimal | None = None,
    hard_total_token_cap: int | None = None,
) -> dict[str, Any]:
    _require_id(workflow_run_id, "workflow_run_id")
    _require_id(component_run_id, "component_run_id")
    if not isinstance(code_revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", code_revision):
        raise D2LCampaignError("code_revision must be a 40-character Git SHA")
    roles = semantic_role_profiles()
    limits = _build_limits(universe, roles)
    if hard_total_token_cap is not None:
        _require_positive_int(hard_total_token_cap, "hard_total_token_cap")
        limits["hard_total_token_cap"] = hard_total_token_cap
        limits["total_token_cap_override"] = True
    else:
        limits["total_token_cap_override"] = False
    if reserved_cost_cap_usd is not None:
        try:
            parsed_cost = Decimal(str(reserved_cost_cap_usd))
        except InvalidOperation as exc:
            raise D2LCampaignError("reserved_cost_cap_usd is invalid") from exc
        if parsed_cost <= 0:
            raise D2LCampaignError("reserved_cost_cap_usd must be positive")
        limits["reserved_cost_cap_usd"] = format(parsed_cost, "f")
        limits["cost_basis"] = "user_declared_hard_budget_provider_usage_if_available"
    transports = initial_transport_sources()
    body = {
        "schema_version": CONFIG_SCHEMA,
        "campaign_version": CAMPAIGN_VERSION,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "component_attempt_id": 1,
        "pipeline_id": PIPELINE_ID,
        "pipeline_version": PIPELINE_VERSION,
        "profile_id": PROFILE_ID,
        "project": {
            "project_id": project.manifest["project_id"],
            "job_id": project.manifest["job_id"],
            "doc_id": project.manifest["document_doc_id"],
        },
        "source_binding": deepcopy(project.source_binding),
        "source_binding_sha256": canonical_sha256(project.source_binding),
        "selected_universe_sha256": _require_sha(
            universe["integrity"]["payload_sha256"], "selected universe sha"
        ),
        "selected_chapter_ids": list(universe["selection"]["selected_chapter_ids"]),
        "stage_ids": list(STAGE_IDS),
        "semantic_roles": roles,
        "transport_sources": transports,
        "transport_policy": {
            "version": TRANSPORT_POLICY_VERSION,
            "retry": deepcopy(TRANSPORT_RETRY_POLICY),
        },
        "limits": limits,
        "state_layout": {
            "work_db": "state/work.sqlite3",
            "component_root": "component",
            "stage_output_root": "component/artifacts",
            "cache_root": "cache",
            "transport_attempt_root": "transport_attempts",
        },
        "resume_policy": {
            "component_run_id_stable": True,
            "component_attempt_id_increments": True,
            "semantic_profile_immutable": True,
            "same_model_transport_change_requires_new_transport_attempt_seal": True,
            "model_change_requires_new_campaign": True,
            "silent_fallback_allowed": False,
        },
        "code_revision": code_revision.lower(),
        "gold_reference_or_score_present": False,
    }
    _reject_sensitive_keys(body)
    return _seal_body(body)


def _artifact_binding(path: Path, *, kind: str, schema: str) -> dict[str, Any]:
    return {
        "relative_path": path.name,
        "artifact_kind": kind,
        "schema_version": schema,
        "sha256": file_sha256(path),
        "sha256_kind": "physical",
    }


def _source_state(project: LoadedProject, *, verify_tree: bool) -> dict[str, Any]:
    rows = _tree_rows(project.package_root) if verify_tree else None
    return {
        "source_manifest_sha256": file_sha256(project.manifest_path),
        "source_db_sha256": file_sha256(project.source_db_path),
        "package_tree_sha256": (
            canonical_sha256(rows) if rows is not None else project.source_snapshot["package_tree_sha256"]
        ),
        "package_file_count": (
            len(rows) if rows is not None else project.source_snapshot["package_file_count"]
        ),
    }


def _source_record_for_role(
    role: Mapping[str, Any], sources: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    source_id = str(role["source_id"])
    source = sources.get(source_id)
    if source is None:
        raise D2LCampaignError(f"role source is not registered: {source_id}")
    return dict(source)


def _transport_attempt_body(
    *,
    campaign_seal_sha256: str,
    role: Mapping[str, Any],
    source_record: Mapping[str, Any],
    component_attempt_id: int,
    transport_attempt_index: int,
    created_at: str,
) -> dict[str, Any]:
    _reject_sensitive_keys(source_record, "source_record")
    required = {
        "source_id",
        "source_revision",
        "source_class",
        "endpoint_class",
        "base_url",
        "adapter_id",
        "protocol",
        "route_id",
        "credential_ref",
        "credential_family",
        "physical_quota_bucket_id",
        "supported_model_ids",
        "output_mode",
        "native_schema_parameter_sent",
    }
    if set(source_record) != required:
        raise D2LCampaignError("transport source record keys mismatch")
    source_id = _require_id(source_record["source_id"], "source_record.source_id")
    if source_id in _RETIRED_SOURCE_IDS:
        raise D2LCampaignError("retired transport source cannot be sealed")
    supported = source_record["supported_model_ids"]
    if not isinstance(supported, list) or role["model_id"] not in supported:
        raise D2LCampaignError("transport source does not support the sealed model")
    if source_record["output_mode"] != "prompt_generated_json":
        raise D2LCampaignError("third-party transport must use prompt-generated JSON")
    if source_record["native_schema_parameter_sent"] is not False:
        raise D2LCampaignError("third-party transport cannot send a native schema")
    _require_positive_int(component_attempt_id, "component_attempt_id")
    _require_positive_int(transport_attempt_index, "transport_attempt_index")
    body = {
        "schema_version": TRANSPORT_SEAL_SCHEMA,
        "campaign_seal_sha256": _require_sha(campaign_seal_sha256, "campaign seal sha"),
        "component_attempt_id": component_attempt_id,
        "transport_attempt_index": transport_attempt_index,
        "role_id": role["role_id"],
        "semantic_role_sha256": role["semantic_role_sha256"],
        "model_id": role["model_id"],
        "source": deepcopy(dict(source_record)),
        "fallback_allowed": False,
        "created_at": created_at,
    }
    return _seal_body(body)


def build_transport_attempt_seal(
    campaign_root: str | Path,
    *,
    role_id: str,
    source_record: Mapping[str, Any],
    component_attempt_id: int,
    transport_attempt_index: int,
    created_at: str | None = None,
) -> dict[str, Any]:
    root = Path(campaign_root).resolve()
    loaded = load_campaign(root)
    config = loaded["config"]
    roles = {row["role_id"]: row for row in config["semantic_roles"]}
    role = roles.get(role_id)
    if role is None:
        raise D2LCampaignError(f"unknown semantic role: {role_id}")
    seal = _transport_attempt_body(
        campaign_seal_sha256=loaded["seal"]["integrity"]["payload_sha256"],
        role=role,
        source_record=source_record,
        component_attempt_id=component_attempt_id,
        transport_attempt_index=transport_attempt_index,
        created_at=created_at or _now(),
    )
    role_slug = role_id.replace(".", "_")
    path = root / "transport_attempts" / role_slug / f"attempt_{transport_attempt_index:04d}.json"
    if path.exists():
        raise D2LCampaignError(f"transport attempt seal already exists: {path}")
    _write_json(path, seal)
    return seal


def load_transport_attempt_seal(
    campaign_root: str | Path,
    *,
    role_id: str,
    transport_attempt_index: int = 1,
) -> dict[str, Any]:
    """Load and rederive one immutable transport-attempt seal."""

    _require_positive_int(transport_attempt_index, "transport_attempt_index")
    loaded = load_campaign(campaign_root)
    config = loaded["config"]
    role = next(
        (row for row in config["semantic_roles"] if row["role_id"] == role_id),
        None,
    )
    if role is None:
        raise D2LCampaignError(f"unknown semantic role: {role_id}")
    role_slug = role_id.replace(".", "_")
    path = (
        loaded["root"]
        / "transport_attempts"
        / role_slug
        / f"attempt_{transport_attempt_index:04d}.json"
    )
    seal = _verify_sealed_payload(
        _load_json(path, "transport attempt seal"),
        schema=TRANSPORT_SEAL_SCHEMA,
        label="transport_attempt_seal",
    )
    expected = _transport_attempt_body(
        campaign_seal_sha256=loaded["seal"]["integrity"]["payload_sha256"],
        role=role,
        source_record=seal.get("source") or {},
        component_attempt_id=seal.get("component_attempt_id"),
        transport_attempt_index=transport_attempt_index,
        created_at=seal.get("created_at"),
    )
    if seal != expected:
        raise D2LCampaignError("transport attempt seal differs from its campaign")
    return seal


def prepare_campaign(
    *,
    job_root: str | Path,
    campaign_root: str | Path,
    workflow_run_id: str,
    component_run_id: str,
    code_revision: str | None = None,
    code_root: str | Path | None = None,
    require_clean_code: bool = True,
    chapter_ids: Sequence[str] | None = None,
    start_chapter: str | None = None,
    end_chapter: str | None = None,
    all_chapters: bool = False,
    reserved_cost_cap_usd: str | Decimal | None = None,
    hard_total_token_cap: int | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    observed_revision = resolve_code_revision(code_root, require_clean=require_clean_code)
    if code_revision is not None and str(code_revision).lower() != observed_revision:
        raise D2LCampaignError("declared code revision does not match runtime Git HEAD")
    code_revision = observed_revision
    project = load_project(job_root, verify_tree=True)
    output = Path(campaign_root).resolve()
    if output.exists():
        raise D2LCampaignError("campaign root must be fresh")
    if output == project.job_root or project.job_root in output.parents or output in project.job_root.parents:
        raise D2LCampaignError("campaign root must not overlap the source job root")
    selection_mode, selected = select_chapters(
        project,
        chapter_ids=chapter_ids,
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        all_chapters=all_chapters,
    )
    source_before = _source_state(project, verify_tree=False)
    catalog = build_chapter_catalog(project)
    universe = build_selected_universe(
        project, selection_mode=selection_mode, selected_chapter_ids=selected
    )
    config = build_campaign_config(
        project,
        universe,
        workflow_run_id=workflow_run_id,
        component_run_id=component_run_id,
        code_revision=code_revision,
        reserved_cost_cap_usd=reserved_cost_cap_usd,
        hard_total_token_cap=hard_total_token_cap,
    )
    timestamp = created_at or _now()
    output.mkdir(parents=True)
    state_dir = output / "state"
    state_dir.mkdir()
    work_db = state_dir / "work.sqlite3"
    shutil.copy2(project.source_db_path, work_db)
    if file_sha256(work_db) != project.source_db_sha256:
        raise D2LCampaignError("work database copy hash drift")
    catalog_path = output / "chapter_catalog.json"
    universe_path = output / "selected_universe.json"
    config_path = output / "campaign_config.json"
    _write_json(catalog_path, catalog)
    _write_json(universe_path, universe)
    _write_json(config_path, config)
    seal_body = {
        "schema_version": SEAL_SCHEMA,
        "campaign_version": CAMPAIGN_VERSION,
        "status": "sealed_0_api",
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "component_attempt_id": 1,
        "selected_chapter_ids": list(selected),
        "source_binding": deepcopy(project.source_binding),
        "source_binding_sha256": canonical_sha256(project.source_binding),
        "campaign_config_sha256": config["integrity"]["payload_sha256"],
        "selected_universe_sha256": universe["integrity"]["payload_sha256"],
        "code_revision": code_revision.lower(),
        "artifacts": {
            "chapter_catalog": _artifact_binding(
                catalog_path, kind="d2l_chapter_catalog", schema=CATALOG_SCHEMA
            ),
            "selected_universe": _artifact_binding(
                universe_path, kind="d2l_selected_universe", schema=UNIVERSE_SCHEMA
            ),
            "campaign_config": _artifact_binding(
                config_path, kind="d2l_campaign_config", schema=CONFIG_SCHEMA
            ),
            "work_db_seed": {
                "relative_path": "state/work.sqlite3",
                "artifact_kind": "isolated_runtime_database",
                "schema_version": "sqlite_runtime_copy_v1",
                "initial_sha256": file_sha256(work_db),
                "sha256_kind": "physical",
                "mutable_after_component_start": True,
            },
        },
        "created_at": timestamp,
    }
    seal = _seal_body(seal_body)
    seal_path = output / "campaign_seal.json"
    _write_json(seal_path, seal)

    transport_refs = []
    sources = config["transport_sources"]
    for role in config["semantic_roles"]:
        attempt = _transport_attempt_body(
            campaign_seal_sha256=seal["integrity"]["payload_sha256"],
            role=role,
            source_record=_source_record_for_role(role, sources),
            component_attempt_id=1,
            transport_attempt_index=1,
            created_at=timestamp,
        )
        role_slug = role["role_id"].replace(".", "_")
        attempt_path = output / "transport_attempts" / role_slug / "attempt_0001.json"
        _write_json(attempt_path, attempt)
        transport_refs.append(
            {
                "role_id": role["role_id"],
                "relative_path": attempt_path.relative_to(output).as_posix(),
                "sha256": file_sha256(attempt_path),
                "sha256_kind": "physical",
            }
        )

    source_after = _source_state(project, verify_tree=True)
    if source_after != source_before:
        raise D2LCampaignError("source project changed during campaign preparation")
    preflight_body = {
        "schema_version": PREFLIGHT_SCHEMA,
        "status": "ready_0_api",
        "zero_api": True,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "project_id": project.manifest["project_id"],
        "job_id": project.manifest["job_id"],
        "selected_chapter_ids": list(selected),
        "selected_block_count": universe["block_count"],
        "channel_counts": universe["channel_counts"],
        "window_counts": {
            "b1": universe["window_estimates"]["b1"]["window_count"],
            "translator_per_arm": universe["window_estimates"]["translator"]["window_count"],
        },
        "limits": deepcopy(config["limits"]),
        "source_before": source_before,
        "source_after": source_after,
        "source_unchanged": True,
        "campaign_seal_sha256": seal["integrity"]["payload_sha256"],
        "transport_attempt_seals": transport_refs,
        "component_plan_status": "not_bound",
        "created_at": timestamp,
    }
    preflight = _seal_body(preflight_body)
    _write_json(output / "preflight_report.json", preflight)
    load_campaign(output)
    return {
        "campaign_root": str(output),
        "campaign_seal_sha256": seal["integrity"]["payload_sha256"],
        "campaign_config_sha256": config["integrity"]["payload_sha256"],
        "selected_universe_sha256": universe["integrity"]["payload_sha256"],
        "selected_chapter_ids": list(selected),
        "selected_block_count": universe["block_count"],
        "channel_counts": universe["channel_counts"],
        "window_counts": preflight["window_counts"],
        "hard_limits": config["limits"],
        "status": "ready_0_api",
    }


def load_campaign(campaign_root: str | Path) -> dict[str, Any]:
    root = Path(campaign_root).resolve()
    seal = _verify_sealed_payload(
        _load_json(root / "campaign_seal.json", "campaign seal"),
        schema=SEAL_SCHEMA,
        label="campaign_seal",
    )
    artifacts = seal.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise D2LCampaignError("campaign seal artifacts are missing")
    values: dict[str, dict[str, Any]] = {}
    schemas = {
        "chapter_catalog": CATALOG_SCHEMA,
        "selected_universe": UNIVERSE_SCHEMA,
        "campaign_config": CONFIG_SCHEMA,
    }
    for key, schema in schemas.items():
        binding = artifacts.get(key)
        if not isinstance(binding, Mapping):
            raise D2LCampaignError(f"campaign seal lacks {key}")
        path = _relative_path(root, binding.get("relative_path"), f"artifacts.{key}")
        if file_sha256(path) != _require_sha(binding.get("sha256"), f"artifacts.{key}.sha256"):
            raise D2LCampaignError(f"campaign artifact hash drift: {key}")
        values[key] = _verify_sealed_payload(
            _load_json(path, key), schema=schema, label=key
        )
    work_binding = artifacts.get("work_db_seed")
    if not isinstance(work_binding, Mapping):
        raise D2LCampaignError("campaign seal lacks work_db_seed")
    work_db = _relative_path(root, work_binding.get("relative_path"), "work_db_seed")
    config = values["campaign_config"]
    universe = values["selected_universe"]
    component_manifest = (
        root
        / str(config["state_layout"]["component_root"])
        / "component_manifest.json"
    )
    if component_manifest.exists():
        try:
            active = validate_component_manifest(
                _load_json(component_manifest, "active component manifest")
            )
        except Exception as exc:
            raise D2LCampaignError("active component manifest is invalid") from exc
        active_identity = {
            "workflow_run_id": active["workflow_run_id"],
            "component_run_id": active["component_run_id"],
            "pipeline_id": active["pipeline_id"],
            "pipeline_version": active["pipeline_version"],
            "source_binding": active["source_binding"],
            "config_sha256": active["config_sha256"],
            "code_revision": active["code_revision"],
            "selected_chapter_ids": active["selected_chapter_ids"],
        }
        expected_active_identity = {
            "workflow_run_id": config["workflow_run_id"],
            "component_run_id": config["component_run_id"],
            "pipeline_id": config["pipeline_id"],
            "pipeline_version": config["pipeline_version"],
            "source_binding": config["source_binding"],
            "config_sha256": config["integrity"]["payload_sha256"],
            "code_revision": config["code_revision"],
            "selected_chapter_ids": config["selected_chapter_ids"],
        }
        if active_identity != expected_active_identity:
            raise D2LCampaignError("active component does not match campaign identity")
    elif file_sha256(work_db) != _require_sha(
        work_binding.get("initial_sha256"), "work_db_seed.initial_sha256"
    ):
        raise D2LCampaignError("work database drifted before component start")
    if config["integrity"]["payload_sha256"] != seal["campaign_config_sha256"]:
        raise D2LCampaignError("campaign config semantic hash drift")
    if universe["integrity"]["payload_sha256"] != seal["selected_universe_sha256"]:
        raise D2LCampaignError("selected universe semantic hash drift")
    if config["source_binding"] != seal["source_binding"]:
        raise D2LCampaignError("campaign source binding drift")
    if config["selected_chapter_ids"] != seal["selected_chapter_ids"]:
        raise D2LCampaignError("campaign chapter selection drift")
    _reject_sensitive_keys({"seal": seal, "config": config, "universe": universe})
    return {
        "root": root,
        "seal": seal,
        "config": config,
        "universe": universe,
        "catalog": values["chapter_catalog"],
        "work_db": work_db,
    }


def bind_component_plan(
    campaign_root: str | Path, plan_mapping: Mapping[str, Any]
) -> dict[str, Any]:
    loaded = load_campaign(campaign_root)
    config = loaded["config"]
    try:
        plan = ComponentPlan.from_mapping(plan_mapping)
    except ComponentRunnerError as exc:
        raise D2LCampaignError(str(exc)) from exc
    expected = {
        "workflow_run_id": config["workflow_run_id"],
        "component_run_id": config["component_run_id"],
        "pipeline_id": config["pipeline_id"],
        "pipeline_version": config["pipeline_version"],
        "source_binding": config["source_binding"],
        "config_sha256": config["integrity"]["payload_sha256"],
        "code_revision": config["code_revision"],
        "selected_chapter_ids": tuple(config["selected_chapter_ids"]),
    }
    observed = {
        "workflow_run_id": plan.workflow_run_id,
        "component_run_id": plan.component_run_id,
        "pipeline_id": plan.pipeline_id,
        "pipeline_version": plan.pipeline_version,
        "source_binding": plan.source_binding,
        "config_sha256": plan.config_sha256,
        "code_revision": plan.code_revision,
        "selected_chapter_ids": plan.selected_chapter_ids,
    }
    if observed != expected:
        raise D2LCampaignError("component plan does not match the sealed campaign")
    path = loaded["root"] / "component_plan.json"
    if path.exists():
        existing = _load_json(path, "component plan")
        if existing != plan.canonical_mapping():
            raise D2LCampaignError("a different component plan is already bound")
        return {
            "component_plan_ref": "component_plan.json",
            "component_plan_sha256": file_sha256(path),
            "runner_plan_sha256": plan.plan_sha256,
            "status": "already_bound",
        }
    _write_json(path, plan.canonical_mapping())
    return {
        "component_plan_ref": "component_plan.json",
        "component_plan_sha256": file_sha256(path),
        "runner_plan_sha256": plan.plan_sha256,
        "status": "bound",
    }


__all__ = [
    "ALLOWED_CHANNELS",
    "B1_TARGET_TOKENS",
    "CAMPAIGN_VERSION",
    "CATALOG_SCHEMA",
    "CONFIG_SCHEMA",
    "D2LCampaignError",
    "LoadedProject",
    "PREFLIGHT_SCHEMA",
    "SEAL_SCHEMA",
    "TRANSLATOR_MAX_BLOCKS",
    "TRANSLATOR_TARGET_TOKENS",
    "TRANSPORT_POLICY_VERSION",
    "TRANSPORT_RETRY_POLICY",
    "TRANSPORT_SEAL_SCHEMA",
    "UNIVERSE_SCHEMA",
    "bind_component_plan",
    "build_campaign_config",
    "build_chapter_catalog",
    "build_selected_universe",
    "build_transport_attempt_seal",
    "initial_transport_sources",
    "load_campaign",
    "load_transport_attempt_seal",
    "load_project",
    "prepare_campaign",
    "resolve_code_revision",
    "select_chapters",
    "semantic_role_profiles",
]
