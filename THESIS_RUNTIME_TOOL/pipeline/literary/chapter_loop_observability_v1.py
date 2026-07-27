"""Console-facing history package for a Literary chapter-loop run.

The event stream stays lightweight.  Full Builder/Auditor changes are stored in
immutable stage artifacts and in one structured delta per completed stage.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
import uuid

from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256


MANIFEST_SCHEMA = "literary_chapter_loop_component_manifest_v2"
ARTIFACT_INDEX_SCHEMA = "literary_chapter_loop_artifact_index_v2"
DELTA_SCHEMA = "literary_chapter_loop_semantic_delta_v1"
EVENT_SCHEMA = "literary_workflow_component_event_v1"

_EVENT_SEVERITY = {
    "component_failed": "error",
    "component_halted": "warning",
    "validation_failed": "error",
    "gate_pause": "warning",
    "warning": "warning",
    "error": "error",
}
_COLLECTION_KEYS_BY_STAGE = {
    "b1_scan": (
        "entity_observations",
        "glossary_observations",
        "review_issues",
        "roster_recognition_proposals",
    ),
    "b1_enrich": (
        "entities",
        "additional_entities",
        "conflict_findings",
        "presence_correction_findings",
        "glossary_items",
        "same_referent_proposals",
        "review_issues",
    ),
    "b1_local_auditor": (
        "decisions",
        "accepted_decisions",
        "revised_decisions",
        "pending_decisions",
        "quarantined_decisions",
        "review_issues",
    ),
    "b1_registry_writer": (
        "cards",
        "entities",
        "relation_edges",
        "pending_hearings",
        "components",
        "review_issues",
    ),
    "xchapter_hearing": (
        "decisions",
        "review_issues",
    ),
    "identity_apply": (
        "entries",
        "effective_entities",
        "pending_components",
        "resolved_components",
    ),
    "b2_frame_interaction": (
        "frame_segments",
        "speaker_turns",
        "salient_events",
        "review_requests",
    ),
    "b2_review_routing": (
        "route_a",
        "route_b",
        "route_c",
        "route_d",
        "route_e",
        "frame_structure_reviews",
    ),
    "speaker_recovery": (
        "overlays",
        "resolved_endpoints",
        "unresolved_tickets",
        "review_issues",
    ),
    "b3_temporal": (
        "effective_state_projection",
        "pending_cases",
        "retained_cases",
        "state_actions",
    ),
    "b3_auditor": (
        "overlays",
        "resolved_pending_case_ids",
        "retained_pending_case_ids",
    ),
    "b3_apply": (
        "effective_state_projection",
        "pending_cases",
        "resolved_pending_case_ids",
        "referred_identity_case_ids",
    ),
    "b0_summary": (
        "entity_refs",
        "frame_refs",
        "event_refs",
        "state_refs",
        "unresolved_case_refs",
    ),
}
_TOKEN_KEYS = {
    "input_tokens",
    "prompt_tokens",
    "output_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
}


class ChapterLoopObservabilityError(RuntimeError):
    pass


class LiteraryChapterLoopHistoryV1:
    def __init__(self, *, run_root: Path, run_id: str) -> None:
        self.root = Path(run_root).resolve()
        self.run_id = str(run_id)
        self.events_path = self.root / "events.jsonl"
        self.manifest_path = self.root / "component_manifest.json"
        self.index_path = self.root / "artifact_index.json"
        self._seq = 0
        self._writer_id = uuid.uuid4().hex[:12]
        if self.events_path.exists():
            rows = _read_jsonl(self.events_path)
            self._seq = len(rows)
            if rows and rows[-1].get("workflow_run_id") != self.run_id:
                raise ChapterLoopObservabilityError(
                    "existing event stream belongs to another run"
                )

    def initialize(
        self,
        *,
        plan_hash: str,
        selected_chapter_ids: Iterable[str],
        code_revision: str,
        binding_hash: str,
        runtime_binding_hash: str | None,
        project_binding: Mapping[str, Any] | None = None,
        project_binding_ref: str | None = None,
    ) -> None:
        chapters = list(selected_chapter_ids)
        project = dict(project_binding or {})
        if not self.manifest_path.exists():
            manifest = {
                "schema_version": MANIFEST_SCHEMA,
                "workflow_run_id": self.run_id,
                "component_id": "translation",
                "component_run_id": self.run_id,
                "component_attempt_id": 1,
                "component_attempt_index": 1,
                "flow_kind": "literary_chapter_loop",
                "pipeline_kind": "literary_chapter_loop",
                "status": "running",
                "plan_hash": plan_hash,
                "selected_chapter_ids": chapters,
                "code_revision": code_revision,
                "active_code_revision": code_revision,
                "code_revision_history": [code_revision],
                "binding_hash": binding_hash,
                "runtime_binding_hash": runtime_binding_hash,
                "project_id": project.get("project_id"),
                "job_id": project.get("job_id"),
                "source_identity_sha256": project.get(
                    "source_identity_sha256"
                ),
                "project_binding_ref": project_binding_ref,
                "project_binding_hash": project.get("binding_hash"),
                "event_log_ref": "events.jsonl",
                "artifact_index_ref": "artifact_index.json",
                "production_publish_performed": False,
            }
            _write_atomic(self.manifest_path, manifest)
            index_body = {
                "schema_version": ARTIFACT_INDEX_SCHEMA,
                "workflow_run_id": self.run_id,
                "artifacts": [],
            }
            _write_atomic(
                self.index_path,
                {**index_body, "index_hash": canonical_hash(index_body)},
            )
            self.emit(
                "component_started",
                stage="__component__",
                agent="system",
                script="run_literary_chapter_cycle_v1.py",
                payload={
                    "plan_hash": plan_hash,
                    "selected_chapter_ids": chapters,
                    "code_revision": code_revision,
                },
            )
            return
        manifest = _read_object(self.manifest_path)
        for key, expected in {
            "workflow_run_id": self.run_id,
            "plan_hash": plan_hash,
            "code_revision": code_revision,
            "binding_hash": binding_hash,
            "runtime_binding_hash": runtime_binding_hash,
            "project_binding_hash": project.get("binding_hash"),
        }.items():
            if manifest.get(key) != expected:
                raise ChapterLoopObservabilityError(
                    f"component manifest drifted at {key}"
                )

    def synchronize_code_revisions(
        self,
        revisions: Iterable[str],
    ) -> None:
        desired = list(revisions)
        if (
            not desired
            or not all(isinstance(value, str) and value for value in desired)
        ):
            raise ChapterLoopObservabilityError(
                "code revision history is malformed"
            )
        manifest = _read_object(self.manifest_path)
        existing_raw = manifest.get("code_revision_history")
        existing = (
            [str(manifest["code_revision"])]
            if existing_raw is None
            else list(existing_raw)
        )
        if (
            not existing
            or not all(isinstance(value, str) and value for value in existing)
            or desired[: len(existing)] != existing
        ):
            raise ChapterLoopObservabilityError(
                "component code revision history is not a lineage prefix"
            )
        if manifest.get("active_code_revision", existing[-1]) != existing[-1]:
            raise ChapterLoopObservabilityError(
                "component active code revision is stale"
            )
        for next_revision in desired[len(existing) :]:
            existing.append(next_revision)
            manifest["active_code_revision"] = next_revision
            manifest["code_revision_history"] = list(existing)
            _write_atomic(self.manifest_path, manifest)

    def emit(
        self,
        event_type: str,
        *,
        stage: str,
        agent: str,
        script: str,
        payload: Mapping[str, Any] | None = None,
        severity: str | None = None,
    ) -> dict[str, Any]:
        self._seq += 1
        local_stage_id = _relay_stage_id(self.root, stage)
        row = {
            "v": 1,
            "schema": EVENT_SCHEMA,
            "event_id": (
                f"{self.run_id}:1:{self._writer_id}:{self._seq:08d}"
            ),
            "workflow_run_id": self.run_id,
            "component_id": "translation",
            "component_run_id": self.run_id,
            "component_attempt_id": 1,
            "component_attempt_index": 1,
            "component_seq": self._seq,
            "run_id": self.run_id,
            "attempt_id": 1,
            "resume_from": None,
            "ts": datetime.now(UTC).isoformat(),
            "stage_id": local_stage_id,
            "stage": local_stage_id,
            "script": str(script),
            "agent": str(agent),
            "event": str(event_type),
            "event_type": str(event_type),
            "severity": str(
                severity or _EVENT_SEVERITY.get(str(event_type), "info")
            ),
            "payload": _json_safe(dict(payload or {})),
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("ab") as handle:
            handle.write((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        return row

    def record_stage(
        self,
        *,
        stage_id: str,
        stage_name: str,
        chapter_id: str,
        stage_root: Path,
        output_names: Iterable[str],
        parent_artifact_refs: Iterable[str],
        report_path: Path | None,
        status: str,
    ) -> dict[str, Any]:
        root = Path(stage_root).resolve()
        parent_refs = list(parent_artifact_refs)
        created: list[dict[str, Any]] = []
        for output_name in output_names:
            for path in _output_files(root / output_name):
                relative_output = path.relative_to(root).as_posix()
                artifact_ref = _artifact_ref(stage_id, relative_output)
                event = self.emit(
                    "artifact_created",
                    stage=stage_id,
                    agent=_agent_for_stage(stage_name),
                    script=stage_name,
                    payload={
                        "artifact_ref": artifact_ref,
                        "artifact_kind": output_name,
                        "relative_path": path.relative_to(self.root).as_posix(),
                        "sha256": file_sha256(path),
                        "parent_artifact_refs": parent_refs,
                    },
                )
                created.append(
                    {
                        "artifact_ref": artifact_ref,
                        "artifact_kind": output_name,
                        "schema_version": _artifact_schema(path),
                        "sha256": file_sha256(path),
                        "sha256_kind": "physical",
                        "relative_path": path.relative_to(self.root).as_posix(),
                        "producer_stage_id": stage_id,
                        "parent_artifact_refs": parent_refs,
                        "created_event_id": event["event_id"],
                    }
                )
        delta = build_stage_semantic_delta_v1(
            run_root=self.root,
            stage_id=stage_id,
            stage_name=stage_name,
            chapter_id=chapter_id,
            stage_root=root,
            output_names=output_names,
        )
        delta_path = root / "semantic_delta.json"
        _write_atomic(delta_path, delta)
        delta_ref = _artifact_ref(stage_id, "semantic_delta.json")
        delta_event = self.emit(
            "artifact_created",
            stage=stage_id,
            agent=_agent_for_stage(stage_name),
            script=stage_name,
            payload={
                "artifact_ref": delta_ref,
                "artifact_kind": "semantic_delta",
                "relative_path": delta_path.relative_to(self.root).as_posix(),
                "sha256": file_sha256(delta_path),
                "parent_artifact_refs": [
                    row["artifact_ref"] for row in created
                ],
            },
        )
        created.append(
            {
                "artifact_ref": delta_ref,
                "artifact_kind": "semantic_delta",
                "schema_version": DELTA_SCHEMA,
                "sha256": file_sha256(delta_path),
                "sha256_kind": "physical",
                "relative_path": delta_path.relative_to(self.root).as_posix(),
                "producer_stage_id": stage_id,
                "parent_artifact_refs": [
                    row["artifact_ref"] for row in created
                ],
                "created_event_id": delta_event["event_id"],
            }
        )
        index = _read_object(self.index_path)
        existing = list(index.get("artifacts") or [])
        refs = {
            row.get("artifact_ref")
            for row in existing
            if isinstance(row, Mapping)
        }
        for row in created:
            if row["artifact_ref"] not in refs:
                existing.append(row)
                refs.add(row["artifact_ref"])
        index_body = {
            "schema_version": ARTIFACT_INDEX_SCHEMA,
            "workflow_run_id": self.run_id,
            "artifacts": existing,
        }
        _write_atomic(
            self.index_path,
            {**index_body, "index_hash": canonical_hash(index_body)},
        )
        usage = extract_usage_v1(report_path) if report_path and report_path.is_file() else {}
        if usage:
            self.emit(
                "usage_snapshot",
                stage=stage_id,
                agent=_agent_for_stage(stage_name),
                script=stage_name,
                payload={
                    "usage": usage,
                    "report_ref": report_path.relative_to(self.root).as_posix(),
                },
            )
        self.emit(
            "stage_done" if status != "skipped" else "stage_skipped",
            stage=stage_id,
            agent=_agent_for_stage(stage_name),
            script=stage_name,
            payload={
                "chapter_id": chapter_id,
                "status": status,
                "artifact_count": len(created),
                "semantic_delta_ref": delta_path.relative_to(self.root).as_posix(),
                "semantic_counts": delta["collection_counts"],
            },
        )
        return {"artifacts": created, "semantic_delta": delta, "usage": usage}

    def record_stage_revision(
        self,
        *,
        stage_id: str,
        stage_name: str,
        chapter_id: str,
        revision_name: str,
        revision_root: Path,
        output_names: Iterable[str],
        parent_artifact_refs: Iterable[str],
        revision_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Index an offline revision under an existing stage without rerunning it."""

        if (
            not revision_name
            or not revision_name.replace("_", "").isalnum()
            or "/" in revision_name
            or "\\" in revision_name
        ):
            raise ChapterLoopObservabilityError(
                "stage revision name is invalid"
            )
        if _relay_stage_id(self.root, stage_id) != stage_id:
            raise ChapterLoopObservabilityError(
                "stage revision cites an unknown stage"
            )
        root = Path(revision_root).resolve()
        try:
            root.relative_to(self.root)
        except ValueError as exc:
            raise ChapterLoopObservabilityError(
                "stage revision root is outside the component"
            ) from exc
        names = tuple(output_names)
        if not names or "semantic_delta.json" in names:
            raise ChapterLoopObservabilityError(
                "stage revision outputs are invalid"
            )
        parent_refs = list(parent_artifact_refs)
        index = _read_object(self.index_path)
        existing = list(index.get("artifacts") or [])
        existing_by_ref = {
            str(row["artifact_ref"]): row
            for row in existing
            if isinstance(row, Mapping)
            and isinstance(row.get("artifact_ref"), str)
        }
        unknown_parents = set(parent_refs) - set(existing_by_ref)
        if unknown_parents:
            raise ChapterLoopObservabilityError(
                "stage revision cites unknown parent artifacts"
            )

        files: list[tuple[str, Path]] = []
        for output_name in names:
            for path in _output_files(root / output_name):
                files.append((output_name, path))
        if not files:
            raise ChapterLoopObservabilityError(
                "stage revision has no output artifacts"
            )
        delta_base = build_stage_semantic_delta_v1(
            run_root=self.root,
            stage_id=stage_id,
            stage_name=stage_name,
            chapter_id=chapter_id,
            stage_root=root,
            output_names=names,
        )
        delta_body = dict(delta_base)
        delta_body.pop("delta_hash", None)
        delta_body["revision_name"] = revision_name
        delta_body["revision_metadata"] = _json_safe(
            dict(revision_metadata or {})
        )
        delta = {**delta_body, "delta_hash": canonical_hash(delta_body)}
        delta_path = root / "semantic_delta.json"
        _write_atomic(delta_path, delta)

        target_refs = [
            _artifact_ref(
                stage_id,
                f"{revision_name}/{path.relative_to(root).as_posix()}",
            )
            for _kind, path in files
        ]
        delta_ref = _artifact_ref(
            stage_id, f"{revision_name}/semantic_delta.json"
        )
        all_target_refs = [*target_refs, delta_ref]
        already_present = [
            artifact_ref
            for artifact_ref in all_target_refs
            if artifact_ref in existing_by_ref
        ]
        if already_present:
            if set(already_present) != set(all_target_refs):
                raise ChapterLoopObservabilityError(
                    "stage revision is only partially indexed"
                )
            for artifact_ref, (_kind, path) in zip(target_refs, files):
                row = existing_by_ref[artifact_ref]
                if (
                    row.get("sha256") != file_sha256(path)
                    or row.get("relative_path")
                    != path.relative_to(self.root).as_posix()
                ):
                    raise ChapterLoopObservabilityError(
                        "indexed stage revision bytes drifted"
                    )
            delta_row = existing_by_ref[delta_ref]
            if (
                delta_row.get("sha256") != file_sha256(delta_path)
                or delta_row.get("relative_path")
                != delta_path.relative_to(self.root).as_posix()
            ):
                raise ChapterLoopObservabilityError(
                    "indexed stage revision delta drifted"
                )
            return {
                "artifacts": [
                    existing_by_ref[artifact_ref]
                    for artifact_ref in all_target_refs
                ],
                "semantic_delta": delta,
                "already_indexed": True,
            }

        created: list[dict[str, Any]] = []
        metadata = _json_safe(dict(revision_metadata or {}))
        for artifact_ref, (output_name, path) in zip(target_refs, files):
            event = self.emit(
                "artifact_created",
                stage=stage_id,
                agent=_agent_for_stage(stage_name),
                script=stage_name,
                payload={
                    "artifact_ref": artifact_ref,
                    "artifact_kind": f"{revision_name}:{output_name}",
                    "relative_path": path.relative_to(self.root).as_posix(),
                    "sha256": file_sha256(path),
                    "parent_artifact_refs": parent_refs,
                    "revision_name": revision_name,
                    "revision_metadata": metadata,
                },
            )
            created.append(
                {
                    "artifact_ref": artifact_ref,
                    "artifact_kind": f"{revision_name}:{output_name}",
                    "schema_version": _artifact_schema(path),
                    "sha256": file_sha256(path),
                    "sha256_kind": "physical",
                    "relative_path": path.relative_to(self.root).as_posix(),
                    "producer_stage_id": stage_id,
                    "parent_artifact_refs": parent_refs,
                    "created_event_id": event["event_id"],
                }
            )
        delta_event = self.emit(
            "artifact_created",
            stage=stage_id,
            agent=_agent_for_stage(stage_name),
            script=stage_name,
            payload={
                "artifact_ref": delta_ref,
                "artifact_kind": "semantic_delta",
                "relative_path": delta_path.relative_to(self.root).as_posix(),
                "sha256": file_sha256(delta_path),
                "parent_artifact_refs": [
                    row["artifact_ref"] for row in created
                ],
                "revision_name": revision_name,
                "revision_metadata": metadata,
            },
        )
        created.append(
            {
                "artifact_ref": delta_ref,
                "artifact_kind": "semantic_delta",
                "schema_version": DELTA_SCHEMA,
                "sha256": file_sha256(delta_path),
                "sha256_kind": "physical",
                "relative_path": delta_path.relative_to(self.root).as_posix(),
                "producer_stage_id": stage_id,
                "parent_artifact_refs": [
                    row["artifact_ref"] for row in created
                ],
                "created_event_id": delta_event["event_id"],
            }
        )
        index_body = {
            "schema_version": ARTIFACT_INDEX_SCHEMA,
            "workflow_run_id": self.run_id,
            "artifacts": [*existing, *created],
        }
        _write_atomic(
            self.index_path,
            {**index_body, "index_hash": canonical_hash(index_body)},
        )
        return {
            "artifacts": created,
            "semantic_delta": delta,
            "already_indexed": False,
        }

    def set_status(self, status: str, *, reason: str | None = None) -> None:
        manifest = _read_object(self.manifest_path)
        manifest["status"] = status
        manifest["updated_at_utc"] = datetime.now(UTC).isoformat()
        if reason is not None:
            manifest["status_reason"] = reason
        else:
            manifest.pop("status_reason", None)
        _write_atomic(self.manifest_path, manifest)


def build_stage_semantic_delta_v1(
    *,
    run_root: Path,
    stage_id: str,
    stage_name: str,
    chapter_id: str,
    stage_root: Path,
    output_names: Iterable[str],
) -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    identifiers: dict[str, list[str]] = {}
    selected_keys = set(_COLLECTION_KEYS_BY_STAGE.get(stage_name, ()))
    for output_name in output_names:
        for path in _output_files(Path(stage_root) / output_name):
            if path.suffix.lower() != ".json":
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            artifact_counts: dict[str, int] = {}
            for key, rows in _walk_collections(value):
                leaf = key.rsplit(".", 1)[-1]
                if selected_keys and leaf not in selected_keys:
                    continue
                artifact_counts[key] = len(rows)
                counts[leaf] += len(rows)
                for row in rows:
                    if isinstance(row, Mapping):
                        status = _row_status(row)
                        if status:
                            statuses[f"{leaf}:{status}"] += 1
                        row_id = _row_id(row)
                        if row_id:
                            identifiers.setdefault(leaf, []).append(row_id)
            snapshots.append(
                {
                    "artifact_ref": path.relative_to(run_root).as_posix(),
                    "sha256": file_sha256(path),
                    "collection_counts": artifact_counts,
                }
            )
    body = {
        "schema_version": DELTA_SCHEMA,
        "stage_id": stage_id,
        "stage_name": stage_name,
        "chapter_id": chapter_id,
        "artifact_snapshots": snapshots,
        "collection_counts": dict(sorted(counts.items())),
        "status_counts": dict(sorted(statuses.items())),
        "identifiers": {
            key: sorted(set(values)) for key, values in sorted(identifiers.items())
        },
    }
    return {**body, "delta_hash": canonical_hash(body)}


def extract_usage_v1(report_path: Path) -> dict[str, int]:
    value = _read_object(Path(report_path))
    totals: Counter[str] = Counter()
    seen: set[int] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            marker = id(node)
            if marker in seen:
                return
            seen.add(marker)
            for key, raw in node.items():
                if (
                    key in _TOKEN_KEYS
                    and isinstance(raw, int)
                    and not isinstance(raw, bool)
                    and raw >= 0
                ):
                    totals[key] += raw
                else:
                    visit(raw)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return dict(sorted(totals.items()))


def _walk_collections(value: Any, prefix: str = "") -> Iterable[tuple[str, list[Any]]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(child, list):
                yield path, child
            elif isinstance(child, Mapping):
                yield from _walk_collections(child, path)


def _row_status(row: Mapping[str, Any]) -> str | None:
    for key in ("status", "state", "verdict", "action", "review_route"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _row_id(row: Mapping[str, Any]) -> str | None:
    preferred = (
        "entity_id",
        "card_id",
        "component_id",
        "pending_case_id",
        "state_id",
        "event_id",
        "turn_id",
        "frame_segment_id",
        "relation_edge_id",
        "observation_id",
        "id",
    )
    for key in preferred:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    for key, value in row.items():
        if key.endswith("_id") and isinstance(value, str) and value:
            return value
    return None


def _agent_for_stage(stage_name: str) -> str:
    if "auditor" in stage_name or "hearing" in stage_name:
        return "auditor"
    if stage_name in {
        "b1_scan",
        "b1_enrich",
        "b2_frame_interaction",
        "b3_temporal",
        "b0_summary",
    }:
        return "builder"
    return "system"


def _relay_stage_id(run_root: Path, stage: str) -> str:
    candidate = str(stage)
    if candidate == "__component__":
        return candidate
    plan_path = Path(run_root) / "run_plan.json"
    if not plan_path.is_file():
        return "__component__"
    plan = _read_object(plan_path)
    known = {
        str(row.get("stage_id"))
        for row in plan.get("stage_plan") or []
        if isinstance(row, Mapping)
    }
    return candidate if candidate in known else "__component__"


def _artifact_ref(stage_id: str, relative_output: str) -> str:
    clean = relative_output.replace("\\", "/").lstrip("/")
    if not clean or ".." in Path(clean).parts:
        raise ChapterLoopObservabilityError("artifact output path is unsafe")
    return f"literary/{stage_id}/{clean}"


def _artifact_schema(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "opaque_json"
    if isinstance(value, Mapping):
        schema = value.get("schema_version")
        if isinstance(schema, str) and schema:
            return schema
    return "json_v1"


def _output_files(path: Path) -> list[Path]:
    target = Path(path)
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(
            candidate
            for candidate in target.rglob("*")
            if candidate.is_file()
        )
    return []


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ChapterLoopObservabilityError("event row must be an object")
        rows.append(value)
    return rows


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ChapterLoopObservabilityError(f"cannot load JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ChapterLoopObservabilityError(f"expected JSON object: {path}")
    return value


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(canonical_json(dict(value)) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(raw) for key, raw in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(raw) for raw in value]
    return str(value)


__all__ = [
    "ChapterLoopObservabilityError",
    "LiteraryChapterLoopHistoryV1",
    "build_stage_semantic_delta_v1",
    "extract_usage_v1",
]
