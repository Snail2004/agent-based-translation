"""Combine sealed B1 cross-chapter hearing batches without calling a model."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256


class B1CrossChapterBatchCombineError(RuntimeError):
    pass


def combine_hearing_batches_v1(
    *, batch_roots: Sequence[Path], output_root: Path
) -> dict[str, Any]:
    roots = [Path(value).resolve() for value in batch_roots]
    output = Path(output_root).resolve()
    if not roots:
        raise B1CrossChapterBatchCombineError("at least one batch root is required")
    if len(set(roots)) != len(roots):
        raise B1CrossChapterBatchCombineError("batch roots repeat")
    if output.exists():
        raise B1CrossChapterBatchCombineError("combined output root already exists")
    if any(output == root or root in output.parents for root in roots):
        raise B1CrossChapterBatchCombineError(
            "combined output may not live inside a source batch"
        )

    queue_hash: str | None = None
    registry_hash: str | None = None
    expected_batch_count: int | None = None
    expected_ready_ids: set[str] | None = None
    seen_batch_indices: set[int] = set()
    seen_component_ids: set[str] = set()
    decisions: list[dict[str, Any]] = []
    request_sources: dict[str, Path] = {}
    source_rows: list[dict[str, Any]] = []

    for root in roots:
        report = _read_object(root / "run_report.json", "batch run report")
        plan = _read_object(root / "preflight_plan.json", "batch preflight plan")
        batch_decisions = _read_list(
            root / "validated_decisions.json", "batch validated decisions"
        )
        if report.get("status") != "semantic_accepted":
            raise B1CrossChapterBatchCombineError(
                "source batch is not semantically accepted"
            )
        if report.get("selected_batch_complete") is not True:
            raise B1CrossChapterBatchCombineError(
                "source batch did not exact-cover its selected components"
            )
        if report.get("quarantined_component_ids") not in ([], ()):
            raise B1CrossChapterBatchCombineError(
                "source batch contains quarantined components"
            )

        current_queue_hash = _text(report.get("queue_hash"), "queue_hash")
        current_registry_hash = _text(report.get("registry_hash"), "registry_hash")
        queue_hash = _same_or_first(queue_hash, current_queue_hash, "queue hash")
        registry_hash = _same_or_first(
            registry_hash, current_registry_hash, "registry hash"
        )
        batch_count = _positive_int(report.get("batch_count"), "batch_count")
        expected_batch_count = _same_or_first(
            expected_batch_count, batch_count, "batch count"
        )
        batch_index = _positive_int(report.get("batch_index"), "batch_index")
        if batch_index > batch_count or batch_index in seen_batch_indices:
            raise B1CrossChapterBatchCombineError(
                "batch indices are repeated or outside their declared range"
            )
        seen_batch_indices.add(batch_index)

        selected_ids = _string_set(
            report.get("accepted_component_ids"), "accepted component ids"
        )
        plan_selected_ids = {
            _text(row.get("component_id"), "plan component id")
            for raw in plan.get("ready_hearings") or []
            for row in [_mapping(raw, "plan hearing")]
        }
        if selected_ids != plan_selected_ids:
            raise B1CrossChapterBatchCombineError(
                "accepted decisions differ from the selected batch plan"
            )
        deferred_ids = _string_set(
            report.get("deferred_ready_component_ids"),
            "deferred ready component ids",
        )
        if selected_ids.intersection(deferred_ids):
            raise B1CrossChapterBatchCombineError(
                "selected and deferred ready components overlap"
            )
        current_expected = selected_ids | deferred_ids
        if expected_ready_ids is None:
            expected_ready_ids = current_expected
        elif current_expected != expected_ready_ids:
            raise B1CrossChapterBatchCombineError(
                "source batches disagree on the complete ready component set"
            )
        if seen_component_ids.intersection(selected_ids):
            raise B1CrossChapterBatchCombineError(
                "one hearing component appears in multiple batches"
            )

        decision_ids = {
            _text(row.get("component_id"), "decision component id")
            for raw in batch_decisions
            for row in [_mapping(raw, "validated decision")]
        }
        if decision_ids != selected_ids or len(decision_ids) != len(batch_decisions):
            raise B1CrossChapterBatchCombineError(
                "validated decisions do not exact-cover their source batch"
            )
        for raw in batch_decisions:
            row = deepcopy(dict(_mapping(raw, "validated decision")))
            component_id = row["component_id"]
            request_sources[component_id] = _unique_component_request(
                root, component_id
            )
            decisions.append(row)
        seen_component_ids.update(selected_ids)
        source_rows.append(
            {
                "batch_index": batch_index,
                "root": str(root),
                "tree_hash": _tree_hash(root),
                "report_hash": _text(report.get("report_hash"), "report_hash"),
                "selected_component_ids": sorted(selected_ids),
            }
        )

    assert expected_batch_count is not None
    assert expected_ready_ids is not None
    if seen_batch_indices != set(range(1, expected_batch_count + 1)):
        raise B1CrossChapterBatchCombineError(
            "source batches do not exact-cover declared batch indices"
        )
    if seen_component_ids != expected_ready_ids:
        raise B1CrossChapterBatchCombineError(
            "source batches do not exact-cover all ready hearing components"
        )

    decisions.sort(key=lambda row: row["component_id"])
    output.mkdir(parents=True, exist_ok=False)
    (output / "validated_decisions.json").write_text(
        canonical_json(decisions) + "\n", encoding="utf-8"
    )
    for component_id in sorted(request_sources):
        target = output / "components" / component_id / "request.json"
        target.parent.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(request_sources[component_id], target)
    source_rows.sort(key=lambda row: row["batch_index"])
    body = {
        "schema_version": "literary_b1_cross_chapter_batch_manifest_v1",
        "queue_hash": queue_hash,
        "registry_hash": registry_hash,
        "batch_count": expected_batch_count,
        "component_ids": sorted(seen_component_ids),
        "decision_count": len(decisions),
        "source_batches": source_rows,
        "provider_calls": 0,
    }
    manifest = {**body, "manifest_hash": canonical_hash(body)}
    (output / "batch_manifest.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8"
    )
    return manifest


def _unique_component_request(root: Path, component_id: str) -> Path:
    matches = [
        path
        for path in (root / "components").glob("*/request.json")
        if _read_object(path, "component request").get("component_id") == component_id
    ]
    if len(matches) != 1:
        raise B1CrossChapterBatchCombineError(
            "validated decision has no unique component request"
        )
    return matches[0]


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise B1CrossChapterBatchCombineError(f"cannot read {label}") from exc
    return dict(_mapping(value, label))


def _read_list(path: Path, label: str) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise B1CrossChapterBatchCombineError(f"cannot read {label}") from exc
    if not isinstance(value, list):
        raise B1CrossChapterBatchCombineError(f"{label} must be a list")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise B1CrossChapterBatchCombineError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise B1CrossChapterBatchCombineError(f"{label} must be non-empty text")
    return value


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise B1CrossChapterBatchCombineError(f"{label} must be a positive integer")
    return value


def _string_set(value: Any, label: str) -> set[str]:
    if not isinstance(value, list):
        raise B1CrossChapterBatchCombineError(f"{label} must be a list")
    result = {_text(item, label) for item in value}
    if len(result) != len(value):
        raise B1CrossChapterBatchCombineError(f"{label} repeat")
    return result


def _same_or_first(existing: Any, current: Any, label: str) -> Any:
    if existing is not None and existing != current:
        raise B1CrossChapterBatchCombineError(f"source batches differ in {label}")
    return current


def _tree_hash(root: Path) -> str:
    return canonical_hash(
        [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": file_sha256(path),
            }
            for path in sorted(value for value in root.rglob("*") if value.is_file())
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    report = combine_hearing_batches_v1(
        batch_roots=args.batch_root, output_root=args.output_root
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
