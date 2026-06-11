import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATASET_FILES
from services.audit_log import log_event
from services.dataset_io import read_json, read_jsonl, write_json_atomic
from services.references import REFERENCE_STATUSES, draft_references
from services.validator import run_validator


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical(project_path: Path) -> Path:
    return project_path / "canonical"


def _document(project_path: Path) -> dict[str, Any]:
    return read_json(_canonical(project_path) / DATASET_FILES["document"])


def _review_state(project_path: Path) -> dict[str, Any]:
    path = project_path / "working" / "review_state.json"
    if not path.exists():
        return {"blocks": {}}
    return read_json(path)


def _metadata_reasons(document: dict[str, Any]) -> list[str]:
    metadata = document.get("metadata", {})
    reasons = []
    for key in ("license", "contamination_risk", "pipeline_version", "source_format"):
        if not metadata.get(key):
            reasons.append(f"missing metadata.{key}")
    if metadata.get("extraction_tool") != "manual-synthetic" and not metadata.get("raw_sha256"):
        reasons.append("missing metadata.raw_sha256")
    return reasons


def freeze_reasons(project_path: Path) -> tuple[list[str], dict[str, Any]]:
    reasons: list[str] = []
    validation = run_validator(_canonical(project_path))
    if not validation.get("ok"):
        errors = validation.get("errors") or []
        reasons.append(f"{len(errors)} validation error(s)")

    document = _document(project_path)
    reasons.extend(_metadata_reasons(document))

    review = _review_state(project_path)
    block_states = review.get("blocks", {})
    all_blocks = [
        block.get("block_id")
        for chapter in document.get("chapters", [])
        for block in chapter.get("blocks", [])
        if block.get("block_id")
    ]
    unreviewed = [bid for bid in all_blocks if not block_states.get(bid, {}).get("reviewed")]
    needs_retag = [bid for bid in all_blocks if block_states.get(bid, {}).get("needs_retag")]
    if unreviewed:
        reasons.append(f"{len(unreviewed)} unreviewed block(s)")
    if needs_retag:
        reasons.append(f"{len(needs_retag)} block(s) need re-tag")

    drafts = draft_references(project_path)
    if drafts:
        reasons.append(f"{len(drafts)} draft reference(s)")

    references = read_jsonl(_canonical(project_path) / DATASET_FILES["manual_reference_subset"])
    if not references:
        reasons.append("no reviewed reference(s)")
    invalid_refs = [row.get("reference_id") for row in references if row.get("status") not in REFERENCE_STATUSES]
    if invalid_refs:
        reasons.append(f"{len(invalid_refs)} reference(s) with invalid status")

    summaries = read_jsonl(_canonical(project_path) / DATASET_FILES["chapter_summaries"])
    covered_chapters = {row.get("chapter_id") for row in summaries if row.get("summary_source") and row.get("source")}
    chapter_ids = {chapter.get("chapter_id") for chapter in document.get("chapters", []) if chapter.get("chapter_id")}
    missing_summaries = sorted(chapter_ids - covered_chapters)
    if missing_summaries:
        reasons.append(f"{len(missing_summaries)} chapter summary item(s) missing")

    return reasons, validation


def _next_freeze_version(project_path: Path) -> str:
    exports = project_path / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    used = set()
    for path in exports.glob("*_v*.zip"):
        stem = path.stem
        version = stem.rsplit("_", 1)[-1]
        used.add(version)
    index = 1
    while True:
        version = f"v{index:03d}"
        if version not in used:
            return version
        index += 1


def _write_zip(project_path: Path, zip_path: Path, include_working: bool, manifest: dict[str, Any]) -> None:
    manifest_path = zip_path.with_name(f"{zip_path.stem}_manifest.json")
    write_json_atomic(manifest_path, manifest)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_name in DATASET_FILES.values():
            path = _canonical(project_path) / file_name
            if path.exists():
                zf.write(path, f"canonical/{file_name}")
        if include_working:
            for path in (project_path / "working").glob("*"):
                if path.is_file():
                    zf.write(path, f"working/{path.name}")
        zf.write(manifest_path, manifest_path.name)


def _add_tree(zf: zipfile.ZipFile, root: Path, arc_root: str, skip_dirs: set[str] | None = None) -> int:
    if not root.exists():
        return 0
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root)
            if skip_dirs and any(part in skip_dirs for part in rel.parts):
                continue
            zf.write(path, f"{arc_root}/{rel.as_posix()}")
            count += 1
    return count


def _block_ids(document: dict[str, Any]) -> list[str]:
    return [
        str(block.get("block_id"))
        for chapter in document.get("chapters", [])
        for block in chapter.get("blocks", [])
        if block.get("block_id")
    ]


def _qc_report(project_path: Path, validation: dict[str, Any] | None = None) -> dict[str, Any]:
    validation = validation or run_validator(_canonical(project_path))
    document = _document(project_path)
    review = _review_state(project_path)
    block_ids = _block_ids(document)
    block_states = review.get("blocks", {})
    reviewed = [bid for bid in block_ids if block_states.get(bid, {}).get("reviewed")]
    needs_retag = [bid for bid in block_ids if block_states.get(bid, {}).get("needs_retag")]
    summaries = read_jsonl(_canonical(project_path) / DATASET_FILES["chapter_summaries"])
    references = read_jsonl(_canonical(project_path) / DATASET_FILES["manual_reference_subset"])
    drafts = draft_references(project_path)
    freeze_blockers, _freeze_validation = freeze_reasons(project_path)
    return {
        "kind": "qc_report",
        "doc_id": project_path.name,
        "generated_at": _now_iso(),
        "schema_version": document.get("schema_version"),
        "metadata": document.get("metadata", {}),
        "validation": {
            "ok": bool(validation.get("ok")),
            "counts": validation.get("counts", {}),
            "errors": validation.get("errors", []),
            "warnings": validation.get("warnings", []),
        },
        "review": {
            "blocks": len(block_ids),
            "reviewed_blocks": len(reviewed),
            "unreviewed_blocks": max(len(block_ids) - len(reviewed), 0),
            "blocks_needing_retag": len(needs_retag),
        },
        "annotations": {
            "glossary_terms": len(read_jsonl(_canonical(project_path) / DATASET_FILES["glossary"])),
            "entities": len(read_jsonl(_canonical(project_path) / DATASET_FILES["entities"])),
            "entity_relations": len(read_jsonl(_canonical(project_path) / DATASET_FILES["entity_relations"])),
            "chapter_summaries": len(summaries),
        },
        "references": {
            "canonical": len(references),
            "reviewed_or_locked": len([row for row in references if row.get("status") in REFERENCE_STATUSES]),
            "drafts": len(drafts),
        },
        "freeze": {
            "ready": not freeze_blockers,
            "blockers": freeze_blockers,
        },
    }


def _add_project_state(zf: zipfile.ZipFile, project_path: Path, include_translation_previews: bool) -> dict[str, int]:
    counts = {"root_files": 0, "canonical_files": 0, "working_files": 0, "other_files": 0}
    for path in sorted(project_path.iterdir()):
        if path.name == "exports":
            continue
        if path.is_file():
            zf.write(path, path.name)
            counts["root_files"] += 1
        elif path.is_dir() and path.name not in {"canonical", "working"}:
            counts["other_files"] += _add_tree(zf, path, path.name)
    counts["canonical_files"] = _add_tree(zf, _canonical(project_path), "canonical")
    skip_working = set() if include_translation_previews else {"translation_preview"}
    counts["working_files"] = _add_tree(zf, project_path / "working", "working", skip_dirs=skip_working)
    return counts


def _write_project_package_zip(
    project_path: Path,
    zip_path: Path,
    manifest: dict[str, Any],
    include_translation_previews: bool,
    validation: dict[str, Any],
) -> None:
    manifest_path = zip_path.with_name(f"{zip_path.stem}_manifest.json")
    write_json_atomic(manifest_path, manifest)
    qc_report = _qc_report(project_path, validation)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        file_counts = _add_project_state(zf, project_path, include_translation_previews=include_translation_previews)
        preview_counts = _preview_counts(project_path) if include_translation_previews else {"inputs": 0, "agent_outputs": 0, "runs": 0}
        preview_count = sum(preview_counts.values())
        zf.writestr("QC_REPORT.json", json.dumps(qc_report, ensure_ascii=False, indent=2) + "\n")
        readme = "\n".join([
            "AI-LAB project export",
            "",
            "This package contains the current source files, canonical dataset, working state, and QC_REPORT.json.",
            "exports/ is intentionally excluded to avoid nesting old export archives.",
            "working/translation_preview/ is included only for dataset_plus_translation_previews exports.",
            "Translation preview data is not gold and must not be treated as manual_reference_subset.",
            f"root_file_count={file_counts['root_files']}",
            f"canonical_file_count={file_counts['canonical_files']}",
            f"working_file_count={file_counts['working_files']}",
            f"other_file_count={file_counts['other_files']}",
            f"translation_preview_file_count={preview_count}",
            "",
        ])
        zf.writestr("README_EXPORT.txt", readme)
        zf.write(manifest_path, manifest_path.name)


def _preview_counts(project_path: Path) -> dict[str, int]:
    root = project_path / "working" / "translation_preview"
    return {
        "inputs": len(list((root / "input").glob("*.json"))) if (root / "input").exists() else 0,
        "agent_outputs": len(list((root / "agent_outputs").glob("*.json"))) if (root / "agent_outputs").exists() else 0,
        "runs": len(list((root / "runs").glob("*.json"))) if (root / "runs").exists() else 0,
    }


def export_project(project_path: Path, user: str = "local") -> dict[str, Any]:
    doc_id = project_path.name
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = project_path / "exports" / f"{doc_id}_dataset_package_{stamp}.zip"
    validation = run_validator(_canonical(project_path))
    manifest = {
        "doc_id": doc_id,
        "kind": "dataset_package",
        "created_at": _now_iso(),
        "validator_ok": bool(validation.get("ok")),
        "counts": validation.get("counts", {}),
        "files": list(DATASET_FILES.values()),
        "qc_report": {"included": True, "path": "QC_REPORT.json"},
        "project_state": {
            "included": True,
            "path": "./",
            "excluded": ["exports/", "working/translation_preview/"],
        },
        "translation_preview": {
            "included": False,
            "preview_only_not_gold": True,
            "counts": {"inputs": 0, "agent_outputs": 0, "runs": 0},
        },
    }
    _write_project_package_zip(project_path, zip_path, manifest, include_translation_previews=False, validation=validation)
    log_event(project_path, "export", {"path": str(zip_path), "validator_ok": manifest["validator_ok"]}, user)
    return {
        "zip": str(zip_path),
        "filename": zip_path.name,
        "download_url": f"/projects/{doc_id}/exports/{zip_path.name}",
        "manifest": str(zip_path.with_name(f"{zip_path.stem}_manifest.json")),
        "manifest_data": manifest,
    }


def export_project_with_previews(project_path: Path, user: str = "local") -> dict[str, Any]:
    doc_id = project_path.name
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = project_path / "exports" / f"{doc_id}_dataset_plus_previews_{stamp}.zip"
    validation = run_validator(_canonical(project_path))
    preview_counts = _preview_counts(project_path)
    manifest = {
        "doc_id": doc_id,
        "kind": "dataset_plus_translation_previews",
        "created_at": _now_iso(),
        "validator_ok": bool(validation.get("ok")),
        "counts": validation.get("counts", {}),
        "files": list(DATASET_FILES.values()),
        "qc_report": {"included": True, "path": "QC_REPORT.json"},
        "project_state": {
            "included": True,
            "path": "./",
            "excluded": ["exports/"],
        },
        "translation_preview": {
            "included": True,
            "preview_only_not_gold": True,
            "counts": preview_counts,
            "path": "working/translation_preview/",
        },
    }
    _write_project_package_zip(project_path, zip_path, manifest, include_translation_previews=True, validation=validation)
    log_event(project_path, "export_with_previews", {
        "path": str(zip_path),
        "validator_ok": manifest["validator_ok"],
        "translation_preview": preview_counts,
    }, user)
    return {
        "zip": str(zip_path),
        "filename": zip_path.name,
        "download_url": f"/projects/{doc_id}/exports/{zip_path.name}",
        "manifest": str(zip_path.with_name(f"{zip_path.stem}_manifest.json")),
        "manifest_data": manifest,
    }


def freeze_project(project_path: Path, user: str = "local") -> dict[str, Any]:
    reasons, validation = freeze_reasons(project_path)
    if reasons:
        return {"frozen": False, "reasons": reasons, "validation": validation}
    doc_id = project_path.name
    version = _next_freeze_version(project_path)
    zip_path = project_path / "exports" / f"{doc_id}_{version}.zip"
    manifest = {
        "doc_id": doc_id,
        "version": version,
        "kind": "freeze",
        "created_at": _now_iso(),
        "validator_ok": True,
        "counts": validation.get("counts", {}),
        "files": list(DATASET_FILES.values()),
    }
    _write_zip(project_path, zip_path, include_working=False, manifest=manifest)
    log_event(project_path, "freeze", {"version": version, "path": str(zip_path)}, user)
    return {"frozen": True, "version": version, "zip": str(zip_path), "manifest": str(zip_path.with_name(f"{zip_path.stem}_manifest.json")), "manifest_data": manifest}
