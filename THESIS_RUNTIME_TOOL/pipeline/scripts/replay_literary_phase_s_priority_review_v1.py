from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.chapter_priority_review_v1 import (
    build_chapter_priority_review_index_v1,
)
from pipeline.literary.checkpoint import (
    canonical_hash,
    canonical_json,
    file_sha256,
    write_checkpoint_atomic,
)


REPORT_SCHEMA_VERSION = "literary_phase_s_priority_review_replay_v1"


class PriorityReplayError(RuntimeError):
    pass


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise PriorityReplayError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise PriorityReplayError(f"{label} must be an object")
    return value


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_file():
        observed = _load(path, "existing replay artifact")
        if canonical_json(observed) != canonical_json(payload):
            raise PriorityReplayError(f"immutable replay differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_checkpoint_atomic(path, dict(payload))


def replay_phase_s_priority_review_v1(
    *, phase_s_root: Path, output_root: Path
) -> dict[str, Any]:
    source_root = Path(phase_s_root).resolve()
    target_root = Path(output_root).resolve()
    canary_dirs = sorted(
        path
        for path in source_root.glob("canary_*")
        if path.is_dir() and (path / "final_report.json").is_file()
    )
    if not canary_dirs:
        raise PriorityReplayError("Phase S root has no completed canaries")
    rows: list[dict[str, Any]] = []
    aggregate: dict[str, int] = {}
    for canary_dir in canary_dirs:
        plan = _load(canary_dir / "run_plan.json", "sealed run plan")
        plan_body = dict(plan)
        observed_plan_hash = plan_body.pop("plan_hash", None)
        if canonical_hash(plan_body) != observed_plan_hash:
            raise PriorityReplayError("sealed run plan hash mismatch")
        final_report = _load(canary_dir / "final_report.json", "final report")
        if final_report.get("status") != "complete_nonproduction_prefix":
            raise PriorityReplayError("replay source canary is incomplete")
        if final_report.get("production_publish_performed") is not False:
            raise PriorityReplayError("replay source claims production publication")
        document_path = Path(str(plan["document_path"]))
        if file_sha256(document_path) != plan.get("document_sha256"):
            raise PriorityReplayError("sealed source document changed")
        document = _load(document_path, "sealed source document")
        ch1_id = str(plan["first_chapter_id"])
        ch2_id = str(plan["second_chapter_id"])
        priority_artifacts = {
            ch1_id: _load(
                canary_dir / "stages" / "ch1_b0" / "live" / "inventory.json",
                "chapter-1 priority artifact",
            ),
            ch2_id: _load(
                canary_dir
                / "stages"
                / "ch2_b0_prior"
                / "live"
                / "prior_challenge_artifact.json",
                "chapter-2 priority artifact",
            ),
        }
        final_prefix = _load(
            canary_dir
            / "artifacts"
            / "chapter_prefix_ch2_identity_reviewed.json",
            "identity-reviewed prefix",
        )
        index = build_chapter_priority_review_index_v1(
            document=document,
            priority_artifacts=priority_artifacts,
            final_prefix_bundle=final_prefix,
        )
        canary_output = target_root / canary_dir.name / "priority_review_index.json"
        _write_immutable(canary_output, index)
        counts = {key: int(value) for key, value in index["counts"].items()}
        for key, value in counts.items():
            aggregate[key] = aggregate.get(key, 0) + value
        rows.append(
            {
                "canary_id": canary_dir.name,
                "source_plan_hash": observed_plan_hash,
                "source_final_report_hash": final_report["report_hash"],
                "priority_review_index_hash": index["priority_review_index_hash"],
                "counts": counts,
                "leads": [
                    {
                        key: deepcopy(lead[key])
                        for key in (
                            "lead_id",
                            "surface_key",
                            "trigger_kinds",
                            "best_rank",
                            "chapter_ids",
                            "subject_prior_card_ids",
                            "route",
                            "lifecycle_state",
                            "authority_effect",
                        )
                    }
                    for lead in index["review_leads"]
                ],
            }
        )
    body = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "source_phase_s_root": str(source_root),
        "canary_count": len(rows),
        "canaries": rows,
        "aggregate_counts": dict(sorted(aggregate.items())),
        "api_calls_performed": 0,
        "production_publish_performed": False,
    }
    report = {**body, "replay_report_hash": canonical_hash(body)}
    _write_immutable(target_root / "priority_review_replay_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay chapter-priority review leads from immutable Phase S artifacts"
    )
    parser.add_argument("--phase-s-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = replay_phase_s_priority_review_v1(
        phase_s_root=args.phase_s_root,
        output_root=args.output_root,
    )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
