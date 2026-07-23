"""Replay-only B1 candidate proposals with no glossary authority."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
)


TIMELINE_SCHEMA = "d2l_candidate_proposal_timeline_v1"
PACKET_SCHEMA = "d2l_candidate_proposal_packet_v1"
STAGE_ID = "b1_candidate_discovery"
LIFECYCLE = "provisional"
SEMANTIC_AUTHORITY = "none"

_FORBIDDEN_KEYS = {
    "api_key",
    "decision",
    "gold",
    "oracle",
    "prompt",
    "raw_prompt",
    "raw_response",
    "reference_text",
    "response_text",
    "secret",
    "target_vi",
    "translation",
}
_PACKET_KEYS = {
    "schema",
    "stage_id",
    "lifecycle",
    "semantic_authority",
    "logical_call_id",
    "logical_call_index",
    "chapter_id",
    "window_id",
    "validation_status",
    "source_block_ids",
    "partition_index",
    "attempt_artifact_refs",
    "request_sha256",
    "validation_binding",
    "attempts",
    "transport_calls",
    "usage",
    "candidate_count",
    "candidates",
    "packet_sha256",
}
_CANDIDATE_KEYS = {
    "candidate_order",
    "observation_id",
    "source_surface",
    "source_block_ids",
    "lifecycle",
    "semantic_authority",
}
_TIMELINE_KEYS = {
    "schema",
    "stage_id",
    "lifecycle",
    "semantic_authority",
    "source_manifest_sha256",
    "campaign_manifest_sha256",
    "candidate_aggregate_sha256",
    "summary",
    "calls",
    "timeline_sha256",
}
_CALL_KEYS = {
    "logical_call_id",
    "logical_call_index",
    "chapter_id",
    "window_id",
    "validation_status",
    "candidate_count",
    "packet_binding",
}
_BINDING_KEYS = {
    "artifact_ref",
    "artifact_kind",
    "schema_version",
    "sha256",
    "sha256_kind",
}


class D2LCandidateProposalTimelineError(ValueError):
    """Raised when provisional candidate replay evidence is not trustworthy."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise D2LCandidateProposalTimelineError(f"{label} must be an object")
    return dict(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise D2LCandidateProposalTimelineError(
            f"{label} must be a non-empty string"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise D2LCandidateProposalTimelineError(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _sha256(value: Any, label: str) -> str:
    digest = _string(value, label).upper()
    if len(digest) != 64 or any(char not in "0123456789ABCDEF" for char in digest):
        raise D2LCandidateProposalTimelineError(f"{label} must be SHA-256")
    return digest


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise D2LCandidateProposalTimelineError(f"{label} must be a string array")
    if len(value) != len(set(value)):
        raise D2LCandidateProposalTimelineError(
            f"{label} must not contain duplicates"
        )
    return list(value)


def _reject_forbidden(value: Any, label: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise D2LCandidateProposalTimelineError(
                    f"{label} contains forbidden key: {key}"
                )
            _reject_forbidden(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{label}[{index}]")


def _unsigned(value: Mapping[str, Any], hash_key: str) -> dict[str, Any]:
    payload = dict(value)
    payload.pop(hash_key, None)
    return payload


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D2LCandidateProposalTimelineError(
            f"Unable to read {label}: {path}"
        ) from exc
    return _mapping(value, label)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    data = canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise D2LCandidateProposalTimelineError(
                f"Existing replay artifact differs: {path}"
            )
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _artifact_path(root: Path, artifact_ref: str, label: str) -> Path:
    relative = Path(_string(artifact_ref, label))
    if relative.is_absolute():
        raise D2LCandidateProposalTimelineError(f"{label} must be relative")
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise D2LCandidateProposalTimelineError(f"{label} escapes artifact root")
    return resolved


def _validate_binding(
    value: Any,
    label: str,
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    binding = _mapping(value, label)
    if set(binding) != _BINDING_KEYS:
        raise D2LCandidateProposalTimelineError(f"{label} keys mismatch")
    _string(binding["artifact_ref"], f"{label}.artifact_ref")
    _string(binding["artifact_kind"], f"{label}.artifact_kind")
    _string(binding["schema_version"], f"{label}.schema_version")
    digest = _sha256(binding["sha256"], f"{label}.sha256")
    if binding["sha256_kind"] != "physical_file_bytes":
        raise D2LCandidateProposalTimelineError(
            f"{label}.sha256_kind must be physical_file_bytes"
        )
    if artifact_root is not None:
        path = _artifact_path(
            artifact_root, str(binding["artifact_ref"]), f"{label}.artifact_ref"
        )
        if not path.is_file() or file_sha256(path) != digest:
            raise D2LCandidateProposalTimelineError(f"{label} hash drift")
    return binding


def validate_candidate_proposal_packet(
    value: Any,
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    packet = _mapping(value, "packet")
    if set(packet) != _PACKET_KEYS or packet.get("schema") != PACKET_SCHEMA:
        raise D2LCandidateProposalTimelineError("packet shape is invalid")
    _reject_forbidden(packet)
    if packet["stage_id"] != STAGE_ID:
        raise D2LCandidateProposalTimelineError("packet stage_id mismatch")
    if packet["lifecycle"] != LIFECYCLE:
        raise D2LCandidateProposalTimelineError("packet must remain provisional")
    if packet["semantic_authority"] != SEMANTIC_AUTHORITY:
        raise D2LCandidateProposalTimelineError(
            "packet must have no semantic authority"
        )
    _string(packet["logical_call_id"], "packet.logical_call_id")
    _integer(packet["logical_call_index"], "packet.logical_call_index", minimum=1)
    _string(packet["chapter_id"], "packet.chapter_id")
    _string(packet["window_id"], "packet.window_id")
    if packet["validation_status"] not in {"valid", "valid_with_warnings"}:
        raise D2LCandidateProposalTimelineError(
            "packet validation_status is not accepted"
        )
    source_blocks = _string_list(packet["source_block_ids"], "packet.source_block_ids")
    _integer(packet["partition_index"], "packet.partition_index", minimum=1)
    _string_list(packet["attempt_artifact_refs"], "packet.attempt_artifact_refs")
    _sha256(packet["request_sha256"], "packet.request_sha256")
    validation_binding = _validate_binding(
        packet["validation_binding"],
        "packet.validation_binding",
        artifact_root=artifact_root,
    )
    if (
        validation_binding["artifact_kind"] != "candidate_validation"
        or validation_binding["schema_version"] != "d2l_candidate_discovery_validator_v2"
    ):
        raise D2LCandidateProposalTimelineError(
            "packet validation binding kind or schema mismatch"
        )
    _integer(packet["attempts"], "packet.attempts", minimum=1)
    _integer(packet["transport_calls"], "packet.transport_calls", minimum=1)
    usage = _mapping(packet["usage"], "packet.usage")
    if set(usage) != {
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_tokens",
    }:
        raise D2LCandidateProposalTimelineError("packet.usage keys mismatch")
    for key in usage:
        _integer(usage[key], f"packet.usage.{key}")

    candidates = packet["candidates"]
    if not isinstance(candidates, list):
        raise D2LCandidateProposalTimelineError("packet.candidates must be an array")
    observation_ids: set[str] = set()
    for index, raw in enumerate(candidates, start=1):
        candidate = _mapping(raw, f"packet.candidates[{index - 1}]")
        if set(candidate) != _CANDIDATE_KEYS:
            raise D2LCandidateProposalTimelineError("candidate keys mismatch")
        if candidate["candidate_order"] != index:
            raise D2LCandidateProposalTimelineError(
                "candidate_order must be contiguous"
            )
        observation_id = _string(
            candidate["observation_id"], "candidate.observation_id"
        )
        if observation_id in observation_ids:
            raise D2LCandidateProposalTimelineError(
                "candidate observation_id must be unique per call"
            )
        observation_ids.add(observation_id)
        _string(candidate["source_surface"], "candidate.source_surface")
        candidate_blocks = _string_list(
            candidate["source_block_ids"], "candidate.source_block_ids"
        )
        if not set(candidate_blocks).issubset(source_blocks):
            raise D2LCandidateProposalTimelineError(
                "candidate source blocks must belong to the active window"
            )
        if candidate["lifecycle"] != "proposed":
            raise D2LCandidateProposalTimelineError(
                "candidate lifecycle must be proposed"
            )
        if candidate["semantic_authority"] != SEMANTIC_AUTHORITY:
            raise D2LCandidateProposalTimelineError(
                "candidate must have no semantic authority"
            )
    if packet["candidate_count"] != len(candidates):
        raise D2LCandidateProposalTimelineError("packet candidate_count mismatch")
    stored_sha = _sha256(packet["packet_sha256"], "packet.packet_sha256")
    if canonical_sha256(_unsigned(packet, "packet_sha256")) != stored_sha:
        raise D2LCandidateProposalTimelineError("packet hash drift")
    return packet


def validate_candidate_proposal_timeline(
    value: Any,
    *,
    artifact_root: Path | None = None,
    expected_window_ids: list[str] | None = None,
) -> dict[str, Any]:
    timeline = _mapping(value, "timeline")
    if set(timeline) != _TIMELINE_KEYS or timeline.get("schema") != TIMELINE_SCHEMA:
        raise D2LCandidateProposalTimelineError("timeline shape is invalid")
    _reject_forbidden(timeline)
    if timeline["stage_id"] != STAGE_ID:
        raise D2LCandidateProposalTimelineError("timeline stage_id mismatch")
    if timeline["lifecycle"] != LIFECYCLE:
        raise D2LCandidateProposalTimelineError("timeline must remain provisional")
    if timeline["semantic_authority"] != SEMANTIC_AUTHORITY:
        raise D2LCandidateProposalTimelineError(
            "timeline must have no semantic authority"
        )
    for key in (
        "source_manifest_sha256",
        "campaign_manifest_sha256",
        "candidate_aggregate_sha256",
    ):
        _sha256(timeline[key], f"timeline.{key}")
    summary = _mapping(timeline["summary"], "timeline.summary")
    if set(summary) != {
        "logical_calls",
        "accepted_calls",
        "calls_with_warnings",
        "provisional_proposals",
        "unique_surfaces",
    }:
        raise D2LCandidateProposalTimelineError("timeline.summary keys mismatch")
    for key in summary:
        _integer(summary[key], f"timeline.summary.{key}")

    calls = timeline["calls"]
    if not isinstance(calls, list):
        raise D2LCandidateProposalTimelineError("timeline.calls must be an array")
    call_ids: set[str] = set()
    window_ids: list[str] = []
    proposal_count = 0
    surfaces: set[str] = set()
    for index, raw in enumerate(calls, start=1):
        call = _mapping(raw, f"timeline.calls[{index - 1}]")
        if set(call) != _CALL_KEYS:
            raise D2LCandidateProposalTimelineError("timeline call keys mismatch")
        if call["logical_call_index"] != index:
            raise D2LCandidateProposalTimelineError(
                "logical call order must be contiguous"
            )
        call_id = _string(call["logical_call_id"], "timeline.call.logical_call_id")
        if call_id in call_ids:
            raise D2LCandidateProposalTimelineError("logical_call_id must be unique")
        call_ids.add(call_id)
        _string(call["chapter_id"], "timeline.call.chapter_id")
        window_id = _string(call["window_id"], "timeline.call.window_id")
        if window_id in window_ids:
            raise D2LCandidateProposalTimelineError("window_id must be unique")
        window_ids.append(window_id)
        if call["validation_status"] not in {"valid", "valid_with_warnings"}:
            raise D2LCandidateProposalTimelineError(
                "timeline call validation_status is not accepted"
            )
        count = _integer(call["candidate_count"], "timeline.call.candidate_count")
        binding = _validate_binding(
            call["packet_binding"],
            "timeline.call.packet_binding",
            artifact_root=artifact_root,
        )
        if (
            binding["artifact_kind"] != "candidate_proposal_packet"
            or binding["schema_version"] != PACKET_SCHEMA
        ):
            raise D2LCandidateProposalTimelineError(
                "candidate packet binding kind or schema mismatch"
            )
        if artifact_root is not None:
            packet_path = _artifact_path(
                artifact_root,
                str(binding["artifact_ref"]),
                "timeline.call.packet_binding.artifact_ref",
            )
            packet = validate_candidate_proposal_packet(
                _load_json(packet_path, "candidate proposal packet"),
                artifact_root=artifact_root,
            )
            if (
                packet["logical_call_id"] != call_id
                or packet["logical_call_index"] != index
                or packet["chapter_id"] != call["chapter_id"]
                or packet["window_id"] != window_id
                or packet["validation_status"] != call["validation_status"]
                or packet["candidate_count"] != count
            ):
                raise D2LCandidateProposalTimelineError(
                    "timeline call does not match bound packet"
                )
            proposal_count += len(packet["candidates"])
            surfaces.update(
                str(candidate["source_surface"])
                for candidate in packet["candidates"]
            )
    if expected_window_ids is not None and window_ids != expected_window_ids:
        raise D2LCandidateProposalTimelineError(
            "timeline does not exact-cover source windows in order"
        )
    if (
        summary["logical_calls"] != len(calls)
        or summary["accepted_calls"] != len(calls)
        or summary["calls_with_warnings"]
        != sum(call["validation_status"] == "valid_with_warnings" for call in calls)
    ):
        raise D2LCandidateProposalTimelineError("timeline call count mismatch")
    if artifact_root is not None:
        if summary["provisional_proposals"] != proposal_count:
            raise D2LCandidateProposalTimelineError(
                "timeline proposal count mismatch"
            )
        if summary["unique_surfaces"] != len(surfaces):
            raise D2LCandidateProposalTimelineError(
                "timeline unique surface count mismatch"
            )
    stored_sha = _sha256(timeline["timeline_sha256"], "timeline.timeline_sha256")
    if canonical_sha256(_unsigned(timeline, "timeline_sha256")) != stored_sha:
        raise D2LCandidateProposalTimelineError("timeline hash drift")
    return timeline


def build_candidate_proposal_timeline(
    *,
    campaign_root: Path,
    source: Mapping[str, Any],
    campaign: Mapping[str, Any],
    aggregate: Mapping[str, Any],
) -> dict[str, Any]:
    root = campaign_root.resolve()
    source_row = _mapping(source, "source")
    campaign_row = _mapping(campaign, "campaign")
    aggregate_row = _mapping(aggregate, "aggregate")
    source_sha = _sha256(source_row.get("manifest_sha256"), "source.manifest_sha256")
    campaign_sha = _sha256(
        campaign_row.get("manifest_sha256"), "campaign.manifest_sha256"
    )
    aggregate_sha = _sha256(
        aggregate_row.get("aggregate_sha256"), "aggregate.aggregate_sha256"
    )
    if aggregate_row.get("source_manifest_sha256") != source_sha:
        raise D2LCandidateProposalTimelineError("aggregate source lineage mismatch")
    if aggregate_row.get("campaign_manifest_sha256") != campaign_sha:
        raise D2LCandidateProposalTimelineError("aggregate campaign lineage mismatch")

    partition_by_window: dict[str, int] = {}
    report_rows: dict[str, dict[str, Any]] = {}
    for raw_partition in campaign_row.get("partitions") or []:
        partition = _mapping(raw_partition, "campaign.partition")
        partition_index = _integer(
            partition.get("partition_index"), "campaign.partition_index", minimum=1
        )
        report_path = root / "partitions" / f"partition_{partition_index:02d}" / "report.json"
        report = _load_json(report_path, "partition report")
        if (report.get("summary") or {}).get("status") != "completed":
            raise D2LCandidateProposalTimelineError(
                f"Partition {partition_index} is not complete"
            )
        rows = report.get("windows") or []
        for raw_window in rows:
            report_window = _mapping(raw_window, "partition report window")
            window_id = _string(report_window.get("window_id"), "report.window_id")
            if window_id in report_rows:
                raise D2LCandidateProposalTimelineError(
                    f"Duplicate report window: {window_id}"
                )
            report_rows[window_id] = report_window
            partition_by_window[window_id] = partition_index

    output_root = root / "candidate_proposal_replay_v1"
    calls: list[dict[str, Any]] = []
    proposal_count = 0
    unique_surfaces: set[str] = set()
    calls_with_warnings = 0
    expected_window_ids: list[str] = []
    for call_index, raw_window in enumerate(source_row.get("windows") or [], start=1):
        source_window = _mapping(raw_window, f"source.windows[{call_index - 1}]")
        window_id = _string(source_window.get("window_id"), "source.window_id")
        expected_window_ids.append(window_id)
        report_window = report_rows.get(window_id)
        if report_window is None or report_window.get("status") not in {
            "valid",
            "valid_with_warnings",
        }:
            raise D2LCandidateProposalTimelineError(
                f"Window has no accepted report row: {window_id}"
            )
        partition_index = partition_by_window[window_id]
        validation_ref = (
            f"partitions/partition_{partition_index:02d}/windows/"
            f"{window_id}/validation.json"
        )
        validation_path = root / validation_ref
        validation = _load_json(validation_path, "candidate validation")
        validation_status = validation.get("status")
        if (
            validation_status not in {"valid", "valid_with_warnings"}
            or report_window.get("status") != validation_status
            or validation.get("window_id") != window_id
        ):
            raise D2LCandidateProposalTimelineError(
                f"Window validation is not accepted: {window_id}"
            )
        validation_sha = file_sha256(validation_path)
        if _sha256(
            report_window.get("validation_sha256"), "report.validation_sha256"
        ) != validation_sha:
            raise D2LCandidateProposalTimelineError(
                f"Window validation hash drift: {window_id}"
            )
        source_block_ids = _string_list(
            source_window.get("block_ids"), "source.window.block_ids"
        )
        proposals: list[dict[str, Any]] = []
        for candidate_order, raw_observation in enumerate(
            validation.get("observations") or [], start=1
        ):
            observation = _mapping(raw_observation, "validation.observation")
            source_surface = _string(
                observation.get("source_surface"), "observation.source_surface"
            )
            unique_surfaces.add(source_surface)
            proposals.append(
                {
                    "candidate_order": candidate_order,
                    "observation_id": _string(
                        observation.get("observation_id"),
                        "observation.observation_id",
                    ),
                    "source_surface": source_surface,
                    "source_block_ids": _string_list(
                        observation.get("source_block_ids"),
                        "observation.source_block_ids",
                    ),
                    "lifecycle": "proposed",
                    "semantic_authority": SEMANTIC_AUTHORITY,
                }
            )
        logical_call_id = f"b1_candidate_call_{call_index:06d}"
        usage = _mapping(report_window.get("usage"), "report.usage")
        packet = {
            "schema": PACKET_SCHEMA,
            "stage_id": STAGE_ID,
            "lifecycle": LIFECYCLE,
            "semantic_authority": SEMANTIC_AUTHORITY,
            "logical_call_id": logical_call_id,
            "logical_call_index": call_index,
            "chapter_id": _string(source_window.get("chapter_id"), "source.chapter_id"),
            "window_id": window_id,
            "validation_status": validation_status,
            "source_block_ids": source_block_ids,
            "partition_index": partition_index,
            "attempt_artifact_refs": _string_list(
                report_window.get("attempt_artifacts") or [],
                "report.attempt_artifacts",
            ),
            "request_sha256": _sha256(
                report_window.get("request_sha256"), "report.request_sha256"
            ),
            "validation_binding": {
                "artifact_ref": validation_ref,
                "artifact_kind": "candidate_validation",
                "schema_version": "d2l_candidate_discovery_validator_v2",
                "sha256": validation_sha,
                "sha256_kind": "physical_file_bytes",
            },
            "attempts": _integer(report_window.get("attempts"), "report.attempts", minimum=1),
            "transport_calls": _integer(
                report_window.get("transport_calls"),
                "report.transport_calls",
                minimum=1,
            ),
            "usage": {
                "prompt_tokens": _integer(usage.get("prompt_tokens"), "usage.prompt_tokens"),
                "completion_tokens": _integer(
                    usage.get("completion_tokens"), "usage.completion_tokens"
                ),
                "reasoning_tokens": _integer(
                    usage.get("reasoning_tokens"), "usage.reasoning_tokens"
                ),
                "cached_tokens": _integer(usage.get("cached_tokens"), "usage.cached_tokens"),
            },
            "candidate_count": len(proposals),
            "candidates": proposals,
        }
        packet["packet_sha256"] = canonical_sha256(packet)
        validate_candidate_proposal_packet(packet, artifact_root=root)
        packet_ref = f"candidate_proposal_replay_v1/calls/{logical_call_id}.json"
        packet_path = root / packet_ref
        _write_json_atomic(packet_path, packet)
        calls.append(
            {
                "logical_call_id": logical_call_id,
                "logical_call_index": call_index,
                "chapter_id": packet["chapter_id"],
                "window_id": window_id,
                "validation_status": validation_status,
                "candidate_count": len(proposals),
                "packet_binding": {
                    "artifact_ref": packet_ref,
                    "artifact_kind": "candidate_proposal_packet",
                    "schema_version": PACKET_SCHEMA,
                    "sha256": file_sha256(packet_path),
                    "sha256_kind": "physical_file_bytes",
                },
            }
        )
        proposal_count += len(proposals)
        calls_with_warnings += int(validation_status == "valid_with_warnings")

    if set(report_rows) != set(expected_window_ids):
        raise D2LCandidateProposalTimelineError(
            "Partition reports do not exact-cover source windows"
        )
    expected_proposals = (aggregate_row.get("summary") or {}).get(
        "accepted_window_observations"
    )
    if expected_proposals != proposal_count:
        raise D2LCandidateProposalTimelineError(
            "Replay proposal count differs from candidate aggregate"
        )
    timeline = {
        "schema": TIMELINE_SCHEMA,
        "stage_id": STAGE_ID,
        "lifecycle": LIFECYCLE,
        "semantic_authority": SEMANTIC_AUTHORITY,
        "source_manifest_sha256": source_sha,
        "campaign_manifest_sha256": campaign_sha,
        "candidate_aggregate_sha256": aggregate_sha,
        "summary": {
            "logical_calls": len(calls),
            "accepted_calls": len(calls),
            "calls_with_warnings": calls_with_warnings,
            "provisional_proposals": proposal_count,
            "unique_surfaces": len(unique_surfaces),
        },
        "calls": calls,
    }
    timeline["timeline_sha256"] = canonical_sha256(timeline)
    validate_candidate_proposal_timeline(
        timeline,
        artifact_root=root,
        expected_window_ids=expected_window_ids,
    )
    _write_json_atomic(output_root / "timeline.json", timeline)
    return timeline


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize replay-only B1 candidate packets from validated output."
    )
    parser.add_argument("--campaign-root", required=True)
    args = parser.parse_args()
    root = Path(args.campaign_root)
    timeline = build_candidate_proposal_timeline(
        campaign_root=root,
        source=_load_json(root / "source_manifest.json", "source manifest"),
        campaign=_load_json(root / "campaign_manifest.json", "campaign manifest"),
        aggregate=_load_json(root / "aggregate.json", "candidate aggregate"),
    )
    print(json.dumps(timeline["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
