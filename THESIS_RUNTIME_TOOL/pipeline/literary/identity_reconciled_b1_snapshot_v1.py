from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping
import uuid

from pipeline.literary.b2_context_v1 import load_real_b1_run_input_v1
from pipeline.literary.chapter_cycle_review_v1 import (
    verify_chapter_cycle_review_ledger_v1,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.incremental_identity_auditor_v1 import (
    verify_incremental_identity_decision_v1,
    verify_incremental_identity_index_v1,
    verify_incremental_identity_ledger_v1,
)
from pipeline.literary.literary_context_pipeline_v1 import tree_hash_v1
from pipeline.literary.review_case_ledger_v1 import verify_review_case_ledger_v1
from pipeline.literary.semantic_candidate_leads_v1 import (
    verify_semantic_identity_occurrence_bridge_v1,
)


SNAPSHOT_SCHEMA_VERSION = "literary_identity_reconciled_b1_snapshot_v1"
ENVELOPE_SCHEMA_VERSION = "literary_identity_reconciled_source_envelope_v1"


class IdentityReconciledSnapshotError(ValueError):
    pass


def materialize_identity_reconciled_b1_snapshot_v1(
    *,
    source_run_root: Path,
    prepare_root: Path,
    recovery_root: Path,
    output_root: Path,
    current_git_head: str,
) -> dict[str, Any]:
    """Materialize a B2-readable B1 snapshot after an audited identity decision.

    The function changes no semantic card fields. It only replaces the final
    prefix and lifecycle ledgers with already-validated recovery artifacts and
    records their full lineage in a new immutable snapshot.
    """

    source = Path(source_run_root).resolve()
    prepare = Path(prepare_root).resolve()
    recovery = Path(recovery_root).resolve()
    output = Path(output_root).resolve()
    head = _required_string(current_git_head, "current_git_head")
    if not source.is_dir() or not prepare.is_dir() or not recovery.is_dir():
        raise IdentityReconciledSnapshotError("identity snapshot source root is absent")
    if output.exists():
        raise IdentityReconciledSnapshotError("identity snapshot output root already exists")
    if output == source or _is_within(output, source):
        raise IdentityReconciledSnapshotError("identity snapshot cannot overwrite its source")

    source_tree_before = tree_hash_v1(source)
    source_input = load_real_b1_run_input_v1(source)
    plan = _read_object(source / "run_plan.json", "source run plan")
    summary = _read_object(source / "run_summary.json", "source run summary")
    _verify_embedded_hash(plan, "plan_hash", "source run plan")
    _verify_embedded_hash(summary, "summary_hash", "source run summary")

    prepare_report = _read_object(prepare / "prepare_report.json", "prepare report")
    recovery_report = _read_object(recovery / "recovery_report.json", "recovery report")
    _verify_embedded_hash(prepare_report, "report_hash", "prepare report")
    _verify_embedded_hash(recovery_report, "report_hash", "recovery report")
    declared_source = Path(
        _required_string(prepare_report.get("source_root"), "prepare source_root")
    ).resolve()
    if declared_source != source:
        raise IdentityReconciledSnapshotError("prepare report belongs to another B1 run")
    if recovery_report.get("source_prepare_report_hash") != prepare_report["report_hash"]:
        raise IdentityReconciledSnapshotError("recovery report points to another prepare report")
    if recovery_report.get("provider_called") is not False:
        raise IdentityReconciledSnapshotError("recovery phase unexpectedly claims an API call")
    if recovery_report.get("production_publish_performed") is not False:
        raise IdentityReconciledSnapshotError("recovery report claims publication")
    if recovery_report.get("mandatory_stop_required") is not False:
        raise IdentityReconciledSnapshotError("recovery report still requires a mandatory stop")

    identity_index = verify_incremental_identity_index_v1(
        _read_object(prepare / "identity_index.json", "identity index")
    )
    if identity_index["identity_index_hash"] != prepare_report.get("identity_index_hash"):
        raise IdentityReconciledSnapshotError("prepare identity index hash is stale")
    decision = verify_incremental_identity_decision_v1(
        _read_object(recovery / "decision.json", "identity decision"),
        index=identity_index,
    )
    if decision["decision_hash"] != recovery_report.get("decision_hash"):
        raise IdentityReconciledSnapshotError("recovery decision hash is stale")
    if decision["status"] != recovery_report.get("decision_status"):
        raise IdentityReconciledSnapshotError("recovery decision status is stale")

    prefix = verify_chapter_prefix_prior_bundle_v1(
        _read_object(recovery / "prefix_post_identity.json", "post-identity prefix")
    )
    review_ledger = verify_chapter_cycle_review_ledger_v1(
        _read_object(
            recovery / "review_ledger_post_identity.json", "post-identity review ledger"
        )
    )
    identity_ledger = verify_incremental_identity_ledger_v1(
        _read_object(recovery / "identity_ledger.json", "identity ledger")
    )
    if decision["decision_hash"] not in {
        row.get("decision_hash") for row in identity_ledger["decision_history"]
    }:
        raise IdentityReconciledSnapshotError(
            "identity ledger does not contain the recovered decision"
        )
    review_case_ledger = verify_review_case_ledger_v1(
        _read_object(
            recovery / "review_case_ledger_post_identity.json",
            "post-identity review-case ledger",
        )
    )
    occurrence_bridge = verify_semantic_identity_occurrence_bridge_v1(
        _read_object(
            prepare / "semantic_identity_occurrence_bridge.json",
            "semantic identity occurrence bridge",
        )
    )
    for observed, expected, label in (
        (prefix["prefix_bundle_hash"], recovery_report.get("prefix_bundle_hash"), "prefix"),
        (
            review_ledger["review_ledger_hash"],
            recovery_report.get("review_ledger_hash"),
            "review ledger",
        ),
        (
            identity_ledger["identity_ledger_hash"],
            recovery_report.get("identity_ledger_hash"),
            "identity ledger",
        ),
        (
            review_case_ledger["review_case_ledger_hash"],
            recovery_report.get("review_case_ledger_hash"),
            "review-case ledger",
        ),
        (
            occurrence_bridge["bridge_hash"],
            prepare_report.get("bridge_hash"),
            "occurrence bridge",
        ),
    ):
        if observed != expected:
            raise IdentityReconciledSnapshotError(f"{label} hash is stale")

    target_chapter_id = _required_string(
        prefix.get("coverage_through_chapter_id"), "coverage_through_chapter_id"
    )
    completed = list(source_input["ordered_chapter_ids"])
    if not completed or target_chapter_id != completed[-1]:
        raise IdentityReconciledSnapshotError(
            "identity recovery must replace the completed-prefix tip"
        )
    source_chapter = next(
        row for row in source_input["chapters"] if row["chapter_id"] == target_chapter_id
    )
    if prefix.get("state_lineage_id") != source_chapter["prefix_bundle"].get(
        "state_lineage_id"
    ):
        raise IdentityReconciledSnapshotError("identity recovery crosses state lineage")
    lineage_ids = {
        prefix.get("state_lineage_id"),
        review_ledger.get("state_lineage_id"),
        identity_ledger.get("state_lineage_id"),
        review_case_ledger.get("state_lineage_id"),
        occurrence_bridge.get("state_lineage_id"),
    }
    if len(lineage_ids) != 1 or None in lineage_ids:
        raise IdentityReconciledSnapshotError("identity recovery ledgers cross lineage")

    provider_artifact = _provider_artifact_path(recovery_report)
    provider_artifact_sha = file_sha256(provider_artifact)
    if provider_artifact_sha != recovery_report.get("source_provider_artifact_sha256"):
        raise IdentityReconciledSnapshotError("provider artifact hash is stale")

    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    temporary.mkdir(parents=True)
    try:
        shutil.copy2(source / "run_plan.json", temporary / "run_plan.json")
        report_index: list[dict[str, Any]] = []
        target_report: dict[str, Any] | None = None
        for chapter in source_input["chapters"]:
            source_report_path = Path(chapter["chapter_report_path"])
            source_report = _read_object(source_report_path, "source chapter report")
            relative_report = source_report_path.relative_to(source)
            target_report_path = temporary / relative_report
            target_report_path.parent.mkdir(parents=True, exist_ok=True)
            if chapter["chapter_id"] == target_chapter_id:
                report_body = deepcopy(source_report)
                report_body.pop("report_hash", None)
                report_body.update(
                    {
                        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
                        "active_context_card_count": len(prefix["b0_context_cards"]),
                        "candidate_only_card_count": len(
                            prefix["candidate_only_context_cards"]
                        ),
                        "glossary_context_card_count": len(
                            prefix["glossary_context_cards"]
                        ),
                        "review_ledger_hash": review_ledger["review_ledger_hash"],
                        "identity_ledger_hash": identity_ledger[
                            "identity_ledger_hash"
                        ],
                        "review_case_ledger_hash": review_case_ledger[
                            "review_case_ledger_hash"
                        ],
                        "semantic_identity_occurrence_bridge_hash": occurrence_bridge[
                            "bridge_hash"
                        ],
                        "identity_reconciliation": {
                            "decision_hash": decision["decision_hash"],
                            "decision_status": decision["status"],
                            "prepare_report_hash": prepare_report["report_hash"],
                            "recovery_report_hash": recovery_report["report_hash"],
                            "provider_artifact_sha256": provider_artifact_sha,
                        },
                    }
                )
                target_report = {
                    **report_body,
                    "report_hash": canonical_hash(report_body),
                }
                _write_new_json(target_report_path, target_report)
                _write_new_json(target_report_path.parent / "final_prefix.json", prefix)
                _write_new_json(
                    target_report_path.parent / "final_review_ledger.json", review_ledger
                )
                _write_new_json(
                    target_report_path.parent / "identity_ledger.json", identity_ledger
                )
                _write_new_json(
                    target_report_path.parent / "final_review_case_ledger.json",
                    review_case_ledger,
                )
                _write_new_json(
                    target_report_path.parent
                    / "semantic_identity_occurrence_bridge.json",
                    occurrence_bridge,
                )
            else:
                shutil.copy2(source_report_path, target_report_path)
                shutil.copy2(
                    Path(chapter["prefix_path"]), target_report_path.parent / "final_prefix.json"
                )
                target_report = source_report
            report_index.append(
                {
                    "chapter_id": chapter["chapter_id"],
                    "path": relative_report.as_posix(),
                    "report_hash": target_report["report_hash"],
                    "prefix_bundle_hash": (
                        prefix["prefix_bundle_hash"]
                        if chapter["chapter_id"] == target_chapter_id
                        else chapter["prefix_bundle_hash"]
                    ),
                }
            )

        provenance_root = temporary / "identity_reconciliation"
        provenance_root.mkdir(parents=True)
        for source_path in (
            prepare / "prepare_report.json",
            prepare / "identity_index.json",
            prepare / "semantic_identity_occurrence_bridge.json",
            recovery / "recovery_report.json",
            recovery / "decision.json",
            recovery / "surface_scope_normalizations.json",
        ):
            shutil.copy2(source_path, provenance_root / source_path.name)
        shutil.copy2(provider_artifact, provenance_root / "provider_response.json")

        summary_body = deepcopy(summary)
        summary_body.pop("summary_hash", None)
        summary_body["state_generation"] = int(summary.get("state_generation") or 0) + 1
        summary_body["run_api_call_count"] = int(summary.get("run_api_call_count") or 0) + 1
        summary_body["semantic_pending_count"] = sum(
            1
            for row in review_ledger["review_items"]
            if row.get("lifecycle_state") != "closed"
        )
        cumulative = dict(summary_body.get("cumulative_hashes") or {})
        cumulative.update(
            {
                "identity_ledger_hash": identity_ledger["identity_ledger_hash"],
                "prefix_hash": prefix["prefix_bundle_hash"],
                "review_case_ledger_hash": review_case_ledger[
                    "review_case_ledger_hash"
                ],
                "review_ledger_hash": review_ledger["review_ledger_hash"],
                "semantic_identity_occurrence_bridge_hash": occurrence_bridge[
                    "bridge_hash"
                ],
            }
        )
        summary_body["cumulative_hashes"] = cumulative
        summary_body["chapter_reports"] = report_index
        summary_body["identity_reconciliation"] = {
            "source_summary_hash": summary["summary_hash"],
            "prepare_report_hash": prepare_report["report_hash"],
            "recovery_report_hash": recovery_report["report_hash"],
            "decision_hash": decision["decision_hash"],
            "decision_status": decision["status"],
            "provider_artifact_sha256": provider_artifact_sha,
        }
        summary_body["state_hash"] = canonical_hash(
            {
                "source_state_hash": summary.get("state_hash"),
                "cumulative_hashes": cumulative,
                "identity_reconciliation": summary_body["identity_reconciliation"],
            }
        )
        derived_summary = {
            **summary_body,
            "summary_hash": canonical_hash(summary_body),
        }
        _write_new_json(temporary / "run_summary.json", derived_summary)

        envelope = {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "git_head": head,
            "source_run_root": str(source),
            "source_run_git_heads": source_input["source_run_git_heads"],
            "source_summary_hash": summary["summary_hash"],
            "derived_summary_hash": derived_summary["summary_hash"],
            "identity_recovery_report_hash": recovery_report["report_hash"],
            "production_publish_performed": False,
        }
        _write_new_json(
            temporary / "stages" / "identity_reconciliation" / "live" / "run_envelope_001.json",
            envelope,
        )

        copied_files = [
            {
                "path": path.relative_to(temporary).as_posix(),
                "sha256": file_sha256(path),
            }
            for path in sorted(row for row in temporary.rglob("*") if row.is_file())
        ]
        manifest_body = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source_run_root": str(source),
            "source_tree_hash": source_tree_before,
            "source_plan_hash": plan["plan_hash"],
            "source_summary_hash": summary["summary_hash"],
            "derived_summary_hash": derived_summary["summary_hash"],
            "target_chapter_id": target_chapter_id,
            "current_git_head": head,
            "prepare_report_hash": prepare_report["report_hash"],
            "recovery_report_hash": recovery_report["report_hash"],
            "decision_hash": decision["decision_hash"],
            "decision_status": decision["status"],
            "provider_artifact_sha256": provider_artifact_sha,
            "files": copied_files,
            "source_artifact_mutated": False,
            "production_publish_performed": False,
        }
        manifest = {
            **manifest_body,
            "snapshot_hash": canonical_hash(manifest_body),
        }
        _write_new_json(temporary / "snapshot_manifest.json", manifest)
        loaded = load_real_b1_run_input_v1(temporary, current_git_head=head)
        if loaded["source_run_git_head"] != head:
            raise IdentityReconciledSnapshotError("derived B1 snapshot head is stale")
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    if tree_hash_v1(source) != source_tree_before:
        raise IdentityReconciledSnapshotError("source B1 run changed during snapshot")
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_root": str(output),
        "snapshot_hash": manifest["snapshot_hash"],
        "summary_hash": derived_summary["summary_hash"],
        "target_chapter_id": target_chapter_id,
        "decision_hash": decision["decision_hash"],
        "decision_status": decision["status"],
        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "input_hash": loaded["input_hash"],
        "certification_eligible": loaded["certification_eligible"],
        "source_artifact_mutated": False,
        "provider_calls_performed": 0,
        "production_publish_performed": False,
    }


def _provider_artifact_path(recovery_report: Mapping[str, Any]) -> Path:
    root = Path(
        _required_string(
            recovery_report.get("source_failed_attempt_root"),
            "source_failed_attempt_root",
        )
    ).resolve()
    digest = _required_string(
        recovery_report.get("source_provider_artifact_sha256"),
        "source_provider_artifact_sha256",
    )
    path = root / "artifacts" / digest[:2] / digest
    if not path.is_file():
        raise IdentityReconciledSnapshotError("provider artifact is absent")
    return path


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityReconciledSnapshotError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise IdentityReconciledSnapshotError(f"{label} must be an object")
    return value


def _verify_embedded_hash(payload: Mapping[str, Any], field: str, label: str) -> None:
    body = dict(payload)
    observed = _required_string(body.pop(field, None), field)
    if canonical_hash(body) != observed:
        raise IdentityReconciledSnapshotError(f"{label} hash mismatch")


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise IdentityReconciledSnapshotError(f"refusing to overwrite artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityReconciledSnapshotError(f"{label} must be a non-empty string")
    return value.strip()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "IdentityReconciledSnapshotError",
    "SNAPSHOT_SCHEMA_VERSION",
    "materialize_identity_reconciled_b1_snapshot_v1",
]
