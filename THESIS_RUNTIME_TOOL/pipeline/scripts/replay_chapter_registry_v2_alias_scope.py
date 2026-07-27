from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.literary.chapter_registry_schema_v2 import (
    ALIAS_SCOPE_POLICY_VERSION,
    RenderedRegistryRequestV2,
)
from pipeline.literary.chapter_registry_v2 import (
    VALIDATOR_VERSION,
    ChapterRegistryStoreV2,
    ChapterWorkingRegistryV2,
    build_registry_generation,
    chapter_source_manifest_hash,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASELINE = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_m4f_b0b1_v2_phase_c_real_20260714_7_gpt54"
)
DEFAULT_DOCUMENT = (
    RUNTIME_ROOT / "data" / "reports" / "literary_l2a0_wh_builder_scaffold" / "document.json"
)
DEFAULT_FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
DEFAULT_CHAPTER_ID = "wh_ch01"
EXPECTED_FROZEN_DB_SHA256 = (
    "64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715"
)
EXPECTED_DOG_ENTITY_ID = "ent2_0a2fc6e82faae509f832"


class ReplayError(RuntimeError):
    """Raised when the persisted lineage cannot be replayed exactly."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new_json(path: Path, payload: Mapping[str, Any] | Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write((canonical_json(payload) + "\n").encode("utf-8"))
        handle.flush()


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _tree_manifest(root: Path) -> dict[str, Any]:
    rows = [
        {"path": path.relative_to(root).as_posix(), "sha256": file_sha256(path)}
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    ]
    return {"files": rows, "tree_hash": canonical_hash(rows)}


def _request_from_artifact(path: Path) -> RenderedRegistryRequestV2:
    wrapper = _read_json(path)
    payload = wrapper.get("registry_request")
    if not isinstance(payload, dict):
        raise ReplayError(f"missing registry_request object: {path}")
    expected = {
        "chapter_id",
        "messages",
        "parent_working_revision_hash",
        "prompt_id",
        "prompt_sha256",
        "request_fingerprint",
        "role",
        "sections",
        "window_id",
    }
    if set(payload) != expected:
        raise ReplayError(f"persisted request shape drift: {path}")
    return RenderedRegistryRequestV2(
        role=str(payload["role"]),
        prompt_id=str(payload["prompt_id"]),
        prompt_sha256=str(payload["prompt_sha256"]),
        chapter_id=str(payload["chapter_id"]),
        window_id=(str(payload["window_id"]) if payload["window_id"] is not None else None),
        parent_working_revision_hash=(
            str(payload["parent_working_revision_hash"])
            if payload["parent_working_revision_hash"] is not None
            else None
        ),
        sections=dict(payload["sections"]),
        messages=tuple(dict(row) for row in payload["messages"]),
        request_fingerprint=str(payload["request_fingerprint"]),
    )


def _chapter(document: Mapping[str, Any], chapter_id: str) -> dict[str, Any]:
    matches = [
        dict(row)
        for row in document.get("chapters") or []
        if str(row.get("chapter_id") or "") == chapter_id
    ]
    if len(matches) != 1:
        raise ReplayError(f"expected one source chapter {chapter_id}, found {len(matches)}")
    return matches[0]


def _call_dirs(baseline: Path, role: str) -> list[Path]:
    rows: list[Path] = []
    for path in sorted((baseline / "calls").iterdir(), key=lambda item: item.name):
        if not path.is_dir() or f"_{role}_" not in path.name:
            continue
        rows.append(path)
    return rows


def _assert_replay(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    entities = list(snapshot.get("entities") or [])
    aliases = list(snapshot.get("aliases") or [])
    bindings = list(snapshot.get("local_bindings") or [])
    dog = [row for row in entities if row.get("entity_id") == EXPECTED_DOG_ENTITY_ID]
    dog_aliases = [row for row in aliases if row.get("entity_id") == EXPECTED_DOG_ENTITY_ID]
    scoped_surfaces = {"madam", "ruffianly bitch"}
    dog_bindings = [
        row
        for row in bindings
        if row.get("target_ref") == EXPECTED_DOG_ENTITY_ID
        and str(row.get("surface") or "").casefold() in scoped_surfaces
    ]
    proper_aliases = [
        row
        for row in aliases
        if str(row.get("surface") or "").casefold() == "heathcliff"
        and row.get("status") == "confirmed"
    ]
    proper_targets = {
        str(row["entity_id"]): row for row in entities if row.get("entity_id") is not None
    }
    checks = [
        ("entity_count_is_7", len(entities) == 7, len(entities)),
        (
            "dog_is_one_confirmed_animal",
            len(dog) == 1
            and dog[0].get("referent_kind") == "animal"
            and dog[0].get("status") == "confirmed",
            dog,
        ),
        ("dog_has_no_global_alias", dog_aliases == [], dog_aliases),
        (
            "dog_local_surfaces_exist_at_b019",
            {(str(row["surface"]).casefold(), row["block_id"]) for row in dog_bindings}
            == {
                ("madam", "wh_ch01_b019"),
                ("ruffianly bitch", "wh_ch01_b019"),
            },
            dog_bindings,
        ),
        (
            "dog_local_surfaces_do_not_escape_b019",
            all(row.get("block_id") == "wh_ch01_b019" for row in dog_bindings),
            dog_bindings,
        ),
        (
            "heathcliff_alias_remains_global",
            len(proper_aliases) == 1
            and proper_targets.get(str(proper_aliases[0]["entity_id"]), {}).get(
                "canonical_surface"
            )
            == "Mr. Heathcliff",
            proper_aliases,
        ),
    ]
    report = [
        {"assertion": name, "passed": passed, "observed": observed}
        for name, passed, observed in checks
    ]
    failed = [row["assertion"] for row in report if not row["passed"]]
    if failed:
        raise ReplayError(f"replay assertions failed: {failed}")
    return report


def replay(
    *,
    baseline: Path,
    document_path: Path,
    output_dir: Path,
    frozen_db: Path,
    chapter_id: str = DEFAULT_CHAPTER_ID,
) -> dict[str, Any]:
    baseline = baseline.resolve()
    document_path = document_path.resolve()
    output_dir = output_dir.resolve()
    frozen_db = frozen_db.resolve()
    if output_dir.exists():
        raise ReplayError(f"append-only replay output already exists: {output_dir}")
    if not baseline.is_dir():
        raise ReplayError(f"missing baseline run: {baseline}")
    if file_sha256(frozen_db).upper() != EXPECTED_FROZEN_DB_SHA256:
        raise ReplayError("frozen D2L database hash drift")

    baseline_before = _tree_manifest(baseline)
    document = _read_json(document_path)
    source_chapter = _chapter(document, chapter_id)
    run_manifest_path = baseline / "run_manifest.json"
    run_manifest = _read_json(run_manifest_path)
    lineage_id = str(run_manifest.get("state_lineage_id") or "")
    if not lineage_id:
        raise ReplayError("baseline manifest has no state_lineage_id")

    working = ChapterWorkingRegistryV2.create(
        state_lineage_id=lineage_id,
        chapter_id=chapter_id,
        source_manifest_hash=chapter_source_manifest_hash(source_chapter),
    )
    input_paths: list[Path] = [document_path, run_manifest_path]
    b1_records: list[dict[str, Any]] = []
    for call_dir in _call_dirs(baseline, "b1"):
        request_path = call_dir / "request.json"
        raw_path = call_dir / "attempt_01" / "raw_result.json"
        request = _request_from_artifact(request_path)
        raw = _read_json(raw_path)
        response = raw.get("parsed_json")
        if not isinstance(response, dict):
            raise ReplayError(f"persisted B1 result has no parsed_json object: {raw_path}")
        application = working.apply_delta(
            request,
            response,
            targeted_recall=":targeted-" in str(request.window_id or ""),
        )
        b1_records.append(
            {
                "call_dir": call_dir.name,
                "request_fingerprint": request.request_fingerprint,
                "response_hash": canonical_hash(response),
                "application_hash": canonical_hash(application),
                "working_revision_hash": working.revision_hash,
            }
        )
        input_paths.extend((request_path, raw_path))

    chapter_root = baseline / "chapters" / chapter_id
    exception_path = chapter_root / "exception_manifest.json"
    audit_path = chapter_root / "audit_decision.json"
    exception_manifest = _read_json(exception_path)
    audit_decision = _read_json(audit_path)
    input_paths.extend((exception_path, audit_path))
    if exception_manifest.get("working_registry_revision_hash") != working.revision_hash:
        raise ReplayError("reconstructed B1 working revision differs from persisted exception manifest")

    b0_dirs = _call_dirs(baseline, "b0")
    auditor_dirs = _call_dirs(baseline, "auditor")
    if len(b0_dirs) != 1 or len(auditor_dirs) != 1:
        raise ReplayError("Chapter-1 replay expects exactly one B0 and one Auditor call")
    b0_request_path = b0_dirs[0] / "request.json"
    auditor_request_path = auditor_dirs[0] / "request.json"
    b0_request = _request_from_artifact(b0_request_path)
    auditor_request = _request_from_artifact(auditor_request_path)
    input_paths.extend((b0_request_path, auditor_request_path))

    generation = build_registry_generation(
        chapter=source_chapter,
        working=working,
        b0_request_fingerprint=b0_request.request_fingerprint,
        exception_manifest=exception_manifest,
        audit_request_fingerprints=[auditor_request.request_fingerprint],
        audit_decision=audit_decision,
    )

    output_dir.mkdir(parents=True, exist_ok=False)
    store = ChapterRegistryStoreV2(output_dir / "registry_store")
    store.commit(generation, expected_parent=None)
    snapshot = store.snapshot(lineage_id, generation.generation_id)
    assertions = _assert_replay(snapshot)
    _write_new_json(output_dir / "prepared_generation.json", generation.to_dict())
    _write_new_json(output_dir / "committed_snapshot.json", snapshot)
    _write_new_json(output_dir / "b1_replay_records.json", b1_records)
    _write_new_json(output_dir / "assertions.json", assertions)

    baseline_after = _tree_manifest(baseline)
    if baseline_after != baseline_before:
        raise ReplayError("accepted baseline artifact changed during replay")
    unique_inputs = sorted(set(path.resolve() for path in input_paths), key=str)
    manifest_body = {
        "schema_version": "chapter_registry_v2_alias_scope_offline_replay_v1",
        "mode": "offline_persisted_response_replay",
        "api_calls": 0,
        "chapter_id": chapter_id,
        "git_commit": _git_head(),
        "validator_version": VALIDATOR_VERSION,
        "policy_versions": {"alias_scope": ALIAS_SCOPE_POLICY_VERSION},
        "baseline": {
            "path": _relative(baseline),
            "tree_hash_before": baseline_before["tree_hash"],
            "tree_hash_after": baseline_after["tree_hash"],
            "file_count": len(baseline_before["files"]),
        },
        "inputs": [
            {"path": _relative(path), "sha256": file_sha256(path)} for path in unique_inputs
        ],
        "frozen_db": {"path": _relative(frozen_db), "sha256": file_sha256(frozen_db)},
        "generation_id": generation.generation_id,
        "generation_hash": canonical_hash(generation.to_dict()),
        "snapshot_hash": snapshot["snapshot_hash"],
        "assertions_hash": canonical_hash(assertions),
    }
    manifest = {
        **manifest_body,
        "manifest_hash": canonical_hash(manifest_body),
        "created_at": _now(),
    }
    _write_new_json(output_dir / "replay_manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay the Chapter-1 alias-scope failure offline")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--chapter-id", default=DEFAULT_CHAPTER_ID)
    args = parser.parse_args()
    manifest = replay(
        baseline=args.baseline,
        document_path=args.document,
        output_dir=args.output_dir,
        frozen_db=args.frozen_db,
        chapter_id=args.chapter_id,
    )
    print(canonical_json(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
