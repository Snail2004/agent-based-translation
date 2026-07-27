"""Build and dry-render a B2 recovery plan without calling an API."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.b2_recovery_v1 import (
    build_b2_recovery_index_v1,
    render_event_review_request_v1,
    render_registry_recovery_request_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


REPORT_SCHEMA_VERSION = "literary_b2_recovery_phase_a_report_v1"


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _request_payload(request: Any) -> dict[str, Any]:
    payload = asdict(request)
    payload["messages"] = list(payload["messages"])
    return payload


def _request_measurement(request: Mapping[str, Any]) -> dict[str, Any]:
    messages_bytes = len(
        canonical_json(request["messages"]).encode("utf-8")
    )
    schema_bytes = len(
        canonical_json(request["response_schema"]).encode("utf-8")
    )
    return {
        "request_kind": request["request_kind"],
        "component_id": request["component_id"],
        "request_fingerprint": request["request_fingerprint"],
        "messages_utf8_bytes": messages_bytes,
        "response_schema_utf8_bytes": schema_bytes,
        "token_estimate": (messages_bytes + schema_bytes + 3) // 4,
    }


def run(*, b2_root: Path, output_root: Path) -> dict[str, Any]:
    if output_root.exists():
        raise FileExistsError(f"output root already exists: {output_root}")
    chapter_artifact = _read_object(
        b2_root / "chapter_b2_artifact.json", "chapter B2 artifact"
    )
    request_paths = sorted(
        (b2_root / "interactions").glob("*/request.json")
    )
    if not request_paths:
        raise RuntimeError("B2 root contains no interaction request artifacts")
    requests = [
        _read_object(path, f"interaction request {path.parent.name}")
        for path in request_paths
    ]
    index = build_b2_recovery_index_v1(
        chapter_artifact=chapter_artifact,
        interaction_requests=requests,
    )
    registry_requests = [
        render_registry_recovery_request_v1(
            index=index, component_id=component["component_id"]
        )
        for component in index["registry_components"]
        if not component["overflow"]
    ]
    event_requests = [
        render_event_review_request_v1(
            index=index,
            component_id=component["component_id"],
            chapter_artifact=chapter_artifact,
            registry_ledger=None,
        )
        for component in index["event_components"]
        if not component["overflow"]
    ]
    _write_new_json(output_root / "recovery_index.json", index)
    measurements: list[dict[str, Any]] = []
    for ordinal, request in enumerate(registry_requests, 1):
        payload = _request_payload(request)
        _write_new_json(
            output_root
            / "registry_requests"
            / f"{ordinal:02d}_{request.component_id}.json",
            payload,
        )
        measurements.append(_request_measurement(payload))
    for ordinal, request in enumerate(event_requests, 1):
        payload = _request_payload(request)
        _write_new_json(
            output_root
            / "event_requests_base"
            / f"{ordinal:02d}_{request.component_id}.json",
            payload,
        )
        measurements.append(_request_measurement(payload))
    body = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "phase": "phase_a_zero_api",
        "source_b2_root": str(b2_root.resolve()),
        "source_b2_artifact_hash": index["source_b2_artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "chapter_id": index["chapter_id"],
        "counts": index["counts"],
        "dry_rendered_requests": measurements,
        "total_token_estimate": sum(
            row["token_estimate"] for row in measurements
        ),
        "api_calls_performed": 0,
        "model_output_authored": False,
        "source_artifact_mutated": False,
        "book_global_identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    report = {**body, "report_hash": canonical_hash(body)}
    _write_new_json(output_root / "phase_a_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--b2-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    report = run(b2_root=args.b2_root, output_root=args.output_root)
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
