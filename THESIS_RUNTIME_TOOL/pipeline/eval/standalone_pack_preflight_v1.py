"""Read-only preflight for a D2LEvaluationInputV1 ZIP handoff."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
from typing import Any, Sequence
from zipfile import ZipFile

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.d2l_input_v1 import validate_d2l_evaluation_input


REQUIRED_PACKAGE_FILES = (
    "package.json",
    "export_receipt.json",
    "input/source_manifest.json",
    "input/runtime_manifest.json",
    "input/runtime_profile.json",
    "input/runtime_glossary.json",
    "input/injection_manifest.json",
    "translations/s0.json",
    "translations/s1.json",
)


def preflight_d2l_evaluation_zip_v1(
    zip_path: Path,
    *,
    expected_chapter_ids: Sequence[str],
) -> dict[str, Any]:
    path = Path(zip_path).resolve()
    if not path.is_file():
        raise ContractValidationError("zip_missing", str(path), "input ZIP is absent")
    expected = tuple(expected_chapter_ids)
    if not expected or len(set(expected)) != len(expected):
        raise ContractValidationError("chapter_selection", "$.expected_chapter_ids", "chapter selection must be nonempty and unique")
    payload_bytes = path.read_bytes()
    with ZipFile(path, "r") as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        normalized_names = [_safe_zip_name(item.filename) for item in files]
        if len(set(normalized_names)) != len(normalized_names):
            raise ContractValidationError("zip_duplicate", str(path), "ZIP repeats a file path")
        prefix = _package_prefix(normalized_names)
        relative_names = [name[len(prefix) :] if prefix else name for name in normalized_names]
        missing = [name for name in REQUIRED_PACKAGE_FILES if name not in relative_names]
        if missing:
            raise ContractValidationError("zip_files", str(path), "missing required files: " + ", ".join(missing))
        package_name = prefix + "package.json"
        try:
            package = json.loads(archive.read(package_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractValidationError("package_json", package_name, "package.json is not canonical UTF-8 JSON") from exc
    validated = validate_d2l_evaluation_input(package)
    observed_chapters = tuple(validated["identity"]["selected_chapter_ids"])
    if observed_chapters != expected:
        raise ContractValidationError(
            "chapter_selection",
            "$.identity.selected_chapter_ids",
            f"expected {list(expected)!r}, observed {list(observed_chapters)!r}",
        )
    arm_ids = tuple(sorted(row["arm_id"] for row in validated["arms"]))
    if arm_ids != ("s0", "s1"):
        raise ContractValidationError("arm_selection", "$.arms", "D2L handoff must contain exactly s0 and s1")
    return {
        "schema_id": "StandaloneD2LPackPreflightV1",
        "schema_version": "1.0.0",
        "status": "accepted",
        "zip_sha256": sha256(payload_bytes).hexdigest(),
        "package_sha256": validated["integrity"]["package_sha256"],
        "package_id": validated["package_id"],
        "selected_chapter_ids": list(observed_chapters),
        "arm_ids": list(arm_ids),
        "block_count": len(validated["blocks"]),
        "translation_row_count": len(validated["translations"]),
        "runtime_term_count": len(validated["runtime_terms"]),
        "zip_root_prefix": prefix,
        "required_files": list(REQUIRED_PACKAGE_FILES),
    }


def _safe_zip_name(value: str) -> str:
    if "\\" in value:
        raise ContractValidationError("zip_path", value, "ZIP paths must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ContractValidationError("zip_path", value, "unsafe ZIP member path")
    return str(path)


def _package_prefix(names: Sequence[str]) -> str:
    if "package.json" in names:
        return ""
    candidates = sorted(name[: -len("package.json")] for name in names if name.endswith("/package.json"))
    if len(candidates) != 1:
        raise ContractValidationError("zip_root", "$", "ZIP must contain exactly one package.json root")
    prefix = candidates[0]
    if any(not name.startswith(prefix) for name in names):
        raise ContractValidationError("zip_root", "$", "all ZIP files must share the package root")
    return prefix
