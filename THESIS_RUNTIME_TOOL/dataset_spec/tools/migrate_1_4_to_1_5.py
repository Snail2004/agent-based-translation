#!/usr/bin/env python3
"""
Migrate an AI-LAB dataset/project from schema 1.4.0 to 1.5.0.

This migration is intentionally narrow:
  - update canonical/document.json schema_version from 1.4.0 to 1.5.0;
  - ensure optional canonical/entity_relations.jsonl exists;
  - do not re-extract, rewrite blocks, or touch working draft/annotation state.

Usage:
    python migrate_1_4_to_1_5.py path/to/project
    python migrate_1_4_to_1_5.py path/to/project --validate
    python migrate_1_4_to_1_5.py path/to/project/canonical --dry-run
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CURRENT_VERSION = "1.5.0"
PREVIOUS_VERSION = "1.4.0"
DOCUMENT_FILE = "document.json"
RELATIONS_FILE = "entity_relations.jsonl"


def _configure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _resolve_dataset_dir(path: Path) -> Path:
    path = path.resolve()
    if (path / DOCUMENT_FILE).is_file():
        return path
    canonical = path / "canonical"
    if (canonical / DOCUMENT_FILE).is_file():
        return canonical
    raise SystemExit(
        "Could not find document.json. Pass either the canonical dataset folder "
        "or the project folder that contains canonical/document.json."
    )


def _load_document(document_path: Path) -> dict:
    try:
        with document_path.open(encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {document_path}: {exc}") from exc


def _write_document_atomic(document_path: Path, document: dict) -> None:
    tmp_path = document_path.with_suffix(document_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(document, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp_path.replace(document_path)


def _backup(document_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = document_path.with_name(f"{document_path.name}.bak-{stamp}")
    shutil.copy2(document_path, backup_path)
    return backup_path


def _run_validator(dataset_dir: Path, schema_dir: Path | None) -> int:
    script_path = Path(__file__).resolve().with_name("validate.py")
    cmd = [sys.executable, str(script_path), "--dataset", str(dataset_dir), "--json"]
    if schema_dir is not None:
        cmd.extend(["--schema", str(schema_dir)])
    proc = subprocess.run(cmd, text=True, encoding="utf-8")
    return proc.returncode


def migrate(path: Path, *, dry_run: bool, no_backup: bool, validate: bool, schema_dir: Path | None) -> int:
    dataset_dir = _resolve_dataset_dir(path)
    document_path = dataset_dir / DOCUMENT_FILE
    relations_path = dataset_dir / RELATIONS_FILE
    document = _load_document(document_path)
    version = document.get("schema_version")
    actions: list[str] = []

    if version == PREVIOUS_VERSION:
        actions.append(f"update {DOCUMENT_FILE} schema_version {PREVIOUS_VERSION} -> {CURRENT_VERSION}")
        document["schema_version"] = CURRENT_VERSION
    elif version == CURRENT_VERSION:
        actions.append(f"{DOCUMENT_FILE} already uses schema_version {CURRENT_VERSION}")
    else:
        raise SystemExit(
            f"Unsupported schema_version {version!r}. This script only migrates "
            f"{PREVIOUS_VERSION} -> {CURRENT_VERSION}."
        )

    if relations_path.exists():
        actions.append(f"{RELATIONS_FILE} already exists")
    else:
        actions.append(f"create optional empty {RELATIONS_FILE}")

    print(f"Dataset: {dataset_dir}")
    for action in actions:
        print(f"- {action}")

    if dry_run:
        print("Dry run only; no files changed.")
        return 0

    backup_path = None
    if version == PREVIOUS_VERSION and not no_backup:
        backup_path = _backup(document_path)
        print(f"Backup: {backup_path}")

    if version == PREVIOUS_VERSION:
        _write_document_atomic(document_path, document)

    if not relations_path.exists():
        relations_path.write_text("", encoding="utf-8")

    if validate:
        return _run_validator(dataset_dir, schema_dir)

    print("Migration complete. Run validate.py to confirm structural validity.")
    return 0


def main() -> int:
    _configure_streams()
    parser = argparse.ArgumentParser(description="Migrate AI-LAB schema 1.4.0 datasets/projects to 1.5.0.")
    parser.add_argument("path", help="Project folder or canonical dataset folder.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions without changing files.")
    parser.add_argument("--no-backup", action="store_true", help="Do not write a document.json backup before changing version.")
    parser.add_argument("--validate", action="store_true", help="Run validate.py after migration.")
    parser.add_argument("--schema", type=Path, default=None, help="Optional schema directory passed to validate.py.")
    args = parser.parse_args()
    return migrate(
        Path(args.path),
        dry_run=args.dry_run,
        no_backup=args.no_backup,
        validate=args.validate,
        schema_dir=args.schema,
    )


if __name__ == "__main__":
    raise SystemExit(main())
