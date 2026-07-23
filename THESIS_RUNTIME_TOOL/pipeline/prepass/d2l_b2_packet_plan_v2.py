from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.prepass.concept_key import normalize_phrase
from pipeline.prepass.d2l_b2_packet_contract_v2 import (
    PROMPT_VERSION,
    RESPONSE_FORMAT,
    RESPONSE_SCHEMA_VERSION,
    prompt_sha256,
    render_messages,
    user_payload_sha256,
)


PLAN_VERSION = "d2l_b2_packet_plan_v2"
INDEX_VERSION = "d2l_candidate_index_v2"
DRY_RENDER_VERSION = "d2l_b2_packet_dry_render_v2"

EXPECTED_SOURCE_MANIFEST_SHA256 = (
    "EDC8DED777A336CC8A8E0062115DB5DA8D94C4DDB36F4FEECC0DA17A10203A43"
)
EXPECTED_AGGREGATE_SHA256 = (
    "92AA86FAB29A433C7C6825A4F02F3ADFFDE580E182EDD49BA9F442046664D615"
)

DEFAULT_MAX_CANDIDATES = 12
DEFAULT_MAX_UNIQUE_BLOCKS = 24
DEFAULT_PROMPT_TOKEN_CAP = 6000
DEFAULT_MAX_EVIDENCE_BLOCKS = 4
OUTPUT_TOKEN_LOW_PER_CANDIDATE = 48
OUTPUT_TOKEN_HIGH_PER_CANDIDATE = 128
OUTPUT_TOKEN_LOW_OVERHEAD = 48
OUTPUT_TOKEN_HIGH_OVERHEAD = 128


class B2PacketPlanError(ValueError):
    pass


@dataclass(frozen=True)
class PacketCaps:
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_unique_blocks: int = DEFAULT_MAX_UNIQUE_BLOCKS
    prompt_token_cap: int = DEFAULT_PROMPT_TOKEN_CAP
    max_evidence_blocks_per_candidate: int = DEFAULT_MAX_EVIDENCE_BLOCKS

    def validate(self) -> None:
        for label, value in (
            ("max_candidates", self.max_candidates),
            ("max_unique_blocks", self.max_unique_blocks),
            ("prompt_token_cap", self.prompt_token_cap),
            (
                "max_evidence_blocks_per_candidate",
                self.max_evidence_blocks_per_candidate,
            ),
        ):
            if not isinstance(value, int) or value <= 0:
                raise B2PacketPlanError(f"{label} must be a positive integer")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest().upper()


def load_sealed_json(
    path: Path,
    *,
    hash_field: str,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise B2PacketPlanError(f"{path} must contain a JSON object")
    recorded = payload.get(hash_field)
    if not isinstance(recorded, str) or not recorded:
        raise B2PacketPlanError(f"{path} has no {hash_field}")
    actual = canonical_sha256(
        {key: value for key, value in payload.items() if key != hash_field}
    )
    if actual != recorded:
        raise B2PacketPlanError(f"{path} content hash mismatch")
    if expected_hash is not None and recorded != expected_hash:
        raise B2PacketPlanError(f"{path} does not match the frozen input hash")
    return payload


def build_candidate_index(
    aggregate: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    *,
    max_evidence_blocks: int = DEFAULT_MAX_EVIDENCE_BLOCKS,
) -> dict[str, Any]:
    if max_evidence_blocks <= 0:
        raise B2PacketPlanError("max_evidence_blocks must be positive")
    block_text, block_order, window_order = _source_indexes(source_manifest)
    source_lineage = str(source_manifest.get("manifest_sha256") or "")
    aggregate_lineage = str(aggregate.get("aggregate_sha256") or "")
    chapter_id = str(source_manifest.get("chapter_id") or "")
    if not source_lineage or not aggregate_lineage or not chapter_id:
        raise B2PacketPlanError("Input lineage or chapter identity is missing")

    raw_rows = aggregate.get("candidates")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise B2PacketPlanError("Aggregate candidates must be a non-empty list")

    groups: dict[str, dict[str, Any]] = {}
    source_row_hashes: set[str] = set()
    for index, raw in enumerate(raw_rows):
        row = _validate_aggregate_row(
            raw,
            index=index,
            block_text=block_text,
            window_order=window_order,
        )
        row_hash = canonical_sha256(row)
        if row_hash in source_row_hashes:
            raise B2PacketPlanError(f"Aggregate row {index} is duplicated")
        source_row_hashes.add(row_hash)
        normalized = normalize_phrase(row["source_surface"])
        if not normalized:
            raise B2PacketPlanError(f"Aggregate row {index} has an empty surface")
        group = groups.setdefault(
            normalized,
            {
                "surfaces": set(),
                "source_block_ids": set(),
                "window_ids": set(),
                "source_row_hashes": [],
            },
        )
        group["surfaces"].add(row["source_surface"])
        group["source_block_ids"].update(row["source_block_ids"])
        group["window_ids"].update(row["window_ids"])
        group["source_row_hashes"].append(row_hash)

    candidates: list[dict[str, Any]] = []
    for normalized, group in groups.items():
        support = sorted(group["source_block_ids"], key=block_order.__getitem__)
        windows = sorted(
            group["window_ids"],
            key=lambda value: (window_order.get(value, 10**9), value),
        )
        surfaces = sorted(
            group["surfaces"],
            key=lambda value: (
                min(
                    block_order[block_id]
                    for row in raw_rows
                    if row.get("source_surface") == value
                    for block_id in row.get("source_block_ids") or []
                ),
                value.casefold(),
                value,
            ),
        )
        evidence = select_ordered_spread(support, max_evidence_blocks)
        candidate_id = "cand_" + canonical_sha256(
            {
                "source_manifest_sha256": source_lineage,
                "normalized_surface": normalized,
            }
        )[:24].lower()
        candidates.append(
            {
                "candidate_id": candidate_id,
                "normalized_surface": normalized,
                "surfaces": surfaces,
                "source_block_ids": support,
                "window_ids": windows,
                "evidence_block_ids": evidence,
                "evidence_complete": len(evidence) == len(support),
                "support_block_count": len(support),
                "window_count": len(windows),
                "source_row_hashes": sorted(group["source_row_hashes"]),
            }
        )

    candidates.sort(
        key=lambda row: (
            block_order[row["source_block_ids"][0]],
            row["normalized_surface"],
            row["candidate_id"],
        )
    )
    candidate_ids = [row["candidate_id"] for row in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise B2PacketPlanError("Candidate ID collision")
    covered_rows = [
        row_hash for row in candidates for row_hash in row["source_row_hashes"]
    ]
    if sorted(covered_rows) != sorted(source_row_hashes):
        raise B2PacketPlanError("Candidate index does not exact-cover aggregate rows")

    result = {
        "index_version": INDEX_VERSION,
        "chapter_id": chapter_id,
        "source_manifest_sha256": source_lineage,
        "aggregate_sha256": aggregate_lineage,
        "normalization_policy": "normalize_phrase_exact_v1",
        "evidence_selection_policy": "ordered_spread_v1",
        "max_evidence_blocks_per_candidate": max_evidence_blocks,
        "summary": {
            "aggregate_rows": len(raw_rows),
            "candidate_rows": len(candidates),
            "grouped_rows": len(raw_rows) - len(candidates),
            "partial_evidence_candidates": sum(
                not row["evidence_complete"] for row in candidates
            ),
            "source_rows_exact_covered": len(covered_rows),
        },
        "candidates": candidates,
    }
    result["candidate_index_sha256"] = canonical_sha256(result)
    return result


def select_ordered_spread(
    ordered_block_ids: Sequence[str], maximum: int
) -> list[str]:
    values = list(ordered_block_ids)
    if maximum <= 0:
        raise B2PacketPlanError("Evidence maximum must be positive")
    if len(values) <= maximum:
        return values
    if maximum == 1:
        return [values[0]]
    denominator = maximum - 1
    positions = [
        (index * (len(values) - 1) + denominator // 2) // denominator
        for index in range(maximum)
    ]
    if len(set(positions)) != maximum:
        raise B2PacketPlanError("Evidence spread did not select unique positions")
    return [values[index] for index in positions]


def build_packet_plan(
    candidate_index: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    *,
    caps: PacketCaps | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    caps = caps or PacketCaps()
    caps.validate()
    block_text, block_order, window_order = _source_indexes(source_manifest)
    candidates = [dict(row) for row in candidate_index.get("candidates") or []]
    if not candidates:
        raise B2PacketPlanError("Candidate index is empty")
    by_id = {str(row["candidate_id"]): row for row in candidates}
    if len(by_id) != len(candidates):
        raise B2PacketPlanError("Candidate index contains duplicate IDs")

    remaining = set(by_id)
    ordered_ids = sorted(
        remaining,
        key=lambda candidate_id: _candidate_order_key(
            by_id[candidate_id], block_order
        ),
    )
    packets: dict[str, dict[str, Any]] = {}
    packet_rows: list[dict[str, Any]] = []

    while remaining:
        seed_id = next(
            candidate_id for candidate_id in ordered_ids if candidate_id in remaining
        )
        selected = [seed_id]
        packet = _render_packet(
            selected,
            by_id=by_id,
            candidate_index=candidate_index,
            block_text=block_text,
            block_order=block_order,
        )
        prompt_tokens = _prompt_tokens(packet)
        _assert_packet_caps(packet, prompt_tokens, caps=caps)

        while len(selected) < caps.max_candidates:
            ranked = sorted(
                (candidate_id for candidate_id in remaining if candidate_id not in selected),
                key=lambda candidate_id: _packing_score(
                    by_id[candidate_id],
                    [by_id[value] for value in selected],
                    block_order=block_order,
                    window_order=window_order,
                ),
            )
            accepted: tuple[str, dict[str, Any], int] | None = None
            for candidate_id in ranked:
                trial_ids = [*selected, candidate_id]
                trial = _render_packet(
                    trial_ids,
                    by_id=by_id,
                    candidate_index=candidate_index,
                    block_text=block_text,
                    block_order=block_order,
                )
                trial_tokens = _prompt_tokens(trial)
                if _packet_fits(trial, trial_tokens, caps=caps):
                    accepted = (candidate_id, trial, trial_tokens)
                    break
            if accepted is None:
                break
            candidate_id, packet, prompt_tokens = accepted
            selected.append(candidate_id)

        selected = sorted(
            selected,
            key=lambda candidate_id: _candidate_order_key(
                by_id[candidate_id], block_order
            ),
        )
        packet = _render_packet(
            selected,
            by_id=by_id,
            candidate_index=candidate_index,
            block_text=block_text,
            block_order=block_order,
        )
        prompt_tokens = _prompt_tokens(packet)
        _assert_packet_caps(packet, prompt_tokens, caps=caps)
        packet_id = str(packet["packet_id"])
        if packet_id in packets:
            raise B2PacketPlanError("Packet ID collision")
        messages = render_messages(packet)
        packet_summary = {
            "packet_id": packet_id,
            "candidate_ids": selected,
            "candidate_count": len(selected),
            "source_block_ids": [row["block_id"] for row in packet["source_blocks"]],
            "source_block_count": len(packet["source_blocks"]),
            "evidence_references": sum(
                len(by_id[candidate_id]["evidence_block_ids"])
                for candidate_id in selected
            ),
            "block_reuse_saved": sum(
                len(by_id[candidate_id]["evidence_block_ids"])
                for candidate_id in selected
            )
            - len(packet["source_blocks"]),
            "prompt_tokens_est": prompt_tokens,
            "completion_tokens_low_est": OUTPUT_TOKEN_LOW_OVERHEAD
            + len(selected) * OUTPUT_TOKEN_LOW_PER_CANDIDATE,
            "completion_tokens_high_est": OUTPUT_TOKEN_HIGH_OVERHEAD
            + len(selected) * OUTPUT_TOKEN_HIGH_PER_CANDIDATE,
            "user_payload_sha256": user_payload_sha256(messages),
        }
        packet_rows.append(packet_summary)
        packets[packet_id] = {
            "packet": packet,
            "messages": messages,
            "summary": packet_summary,
        }
        remaining.difference_update(selected)

    owner_ids = [candidate_id for row in packet_rows for candidate_id in row["candidate_ids"]]
    if len(owner_ids) != len(set(owner_ids)) or set(owner_ids) != set(by_id):
        raise B2PacketPlanError("Packet plan does not exact-cover candidate IDs")

    plan = {
        "plan_version": PLAN_VERSION,
        "chapter_id": candidate_index["chapter_id"],
        "candidate_index_sha256": candidate_index["candidate_index_sha256"],
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "packing_policy": "evidence_then_window_then_source_order_v1",
        "caps": {
            "max_candidates": caps.max_candidates,
            "max_unique_blocks": caps.max_unique_blocks,
            "prompt_token_cap": caps.prompt_token_cap,
            "max_evidence_blocks_per_candidate": caps.max_evidence_blocks_per_candidate,
        },
        "summary": _dry_summary(
            packet_rows,
            candidate_count=len(candidates),
            aggregate_row_count=int(
                candidate_index["summary"]["aggregate_rows"]
            ),
            grouped_row_count=int(candidate_index["summary"]["grouped_rows"]),
            partial_evidence_candidates=sum(
                not row["evidence_complete"] for row in candidates
            ),
        ),
        "packets": packet_rows,
    }
    plan["packet_plan_sha256"] = canonical_sha256(plan)
    return plan, packets


def write_plan(
    *,
    out_dir: Path,
    candidate_index: Mapping[str, Any],
    packet_plan: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_or_verify_json(out_dir / "candidate_index.json", candidate_index)
    _write_or_verify_json(out_dir / "packet_plan.json", packet_plan)
    for packet_id, payload in sorted(packets.items()):
        _write_or_verify_json(
            out_dir / "packets" / packet_id / "request.json", payload
        )
    dry = {
        "dry_render_version": DRY_RENDER_VERSION,
        "source_manifest_sha256": candidate_index["source_manifest_sha256"],
        "aggregate_sha256": candidate_index["aggregate_sha256"],
        "candidate_index_sha256": candidate_index["candidate_index_sha256"],
        "packet_plan_sha256": packet_plan["packet_plan_sha256"],
        "summary": packet_plan["summary"],
        "no_api_called": True,
        "gold_loaded": False,
        "historical_notebook_loaded": False,
    }
    dry["dry_render_sha256"] = canonical_sha256(dry)
    _write_or_verify_json(out_dir / "dry_render.json", dry)
    return dry


def plan_from_paths(
    *,
    aggregate_path: Path,
    source_manifest_path: Path,
    out_dir: Path,
    caps: PacketCaps | None = None,
    expected_aggregate_sha256: str | None = EXPECTED_AGGREGATE_SHA256,
    expected_source_manifest_sha256: str | None = EXPECTED_SOURCE_MANIFEST_SHA256,
) -> dict[str, Any]:
    aggregate = load_sealed_json(
        aggregate_path,
        hash_field="aggregate_sha256",
        expected_hash=expected_aggregate_sha256,
    )
    source = load_sealed_json(
        source_manifest_path,
        hash_field="manifest_sha256",
        expected_hash=expected_source_manifest_sha256,
    )
    caps = caps or PacketCaps()
    index = build_candidate_index(
        aggregate,
        source,
        max_evidence_blocks=caps.max_evidence_blocks_per_candidate,
    )
    plan, packets = build_packet_plan(index, source, caps=caps)
    return write_plan(
        out_dir=out_dir,
        candidate_index=index,
        packet_plan=plan,
        packets=packets,
    )


def _source_indexes(
    source_manifest: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, int], dict[str, int]]:
    windows = source_manifest.get("windows")
    if not isinstance(windows, list) or not windows:
        raise B2PacketPlanError("Source manifest windows are missing")
    block_text: dict[str, str] = {}
    block_order: dict[str, int] = {}
    window_order: dict[str, int] = {}
    for window_position, window in enumerate(windows):
        if not isinstance(window, dict):
            raise B2PacketPlanError("Source manifest window is invalid")
        window_id = str(window.get("window_id") or "")
        if not window_id or window_id in window_order:
            raise B2PacketPlanError("Source manifest window ID is invalid")
        window_order[window_id] = int(window.get("window_order", window_position))
        for raw in window.get("source_blocks") or []:
            if not isinstance(raw, list) or len(raw) != 2:
                raise B2PacketPlanError("Source block row is invalid")
            block_id, text = str(raw[0]), str(raw[1])
            if not block_id or not text:
                raise B2PacketPlanError("Source block content is invalid")
            if block_id in block_text and block_text[block_id] != text:
                raise B2PacketPlanError("Source block bytes disagree across windows")
            if block_id not in block_text:
                block_order[block_id] = len(block_order)
                block_text[block_id] = text
    return block_text, block_order, window_order


def _validate_aggregate_row(
    raw: Any,
    *,
    index: int,
    block_text: Mapping[str, str],
    window_order: Mapping[str, int],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "source_surface",
        "source_block_ids",
        "window_ids",
    }:
        raise B2PacketPlanError(f"Aggregate candidate {index} has invalid shape")
    surface = raw.get("source_surface")
    block_ids = raw.get("source_block_ids")
    window_ids = raw.get("window_ids")
    if not isinstance(surface, str) or not surface.strip():
        raise B2PacketPlanError(f"Aggregate candidate {index} surface is invalid")
    if not isinstance(block_ids, list) or not block_ids or not all(
        isinstance(value, str) and value in block_text for value in block_ids
    ):
        raise B2PacketPlanError(f"Aggregate candidate {index} support is invalid")
    if not isinstance(window_ids, list) or not window_ids or not all(
        isinstance(value, str) and value in window_order for value in window_ids
    ):
        raise B2PacketPlanError(f"Aggregate candidate {index} windows are invalid")
    if len(block_ids) != len(set(block_ids)) or len(window_ids) != len(set(window_ids)):
        raise B2PacketPlanError(f"Aggregate candidate {index} repeats provenance")
    return {
        "source_surface": surface,
        "source_block_ids": list(block_ids),
        "window_ids": list(window_ids),
    }


def _candidate_order_key(
    row: Mapping[str, Any], block_order: Mapping[str, int]
) -> tuple[int, str, str]:
    return (
        min(block_order[value] for value in row["source_block_ids"]),
        str(row["normalized_surface"]),
        str(row["candidate_id"]),
    )


def _packing_score(
    candidate: Mapping[str, Any],
    selected: Sequence[Mapping[str, Any]],
    *,
    block_order: Mapping[str, int],
    window_order: Mapping[str, int],
) -> tuple[int, int, int, int, str]:
    selected_blocks = {
        value for row in selected for value in row["evidence_block_ids"]
    }
    selected_windows = {value for row in selected for value in row["window_ids"]}
    candidate_blocks = set(candidate["evidence_block_ids"])
    candidate_windows = set(candidate["window_ids"])
    block_overlap = len(selected_blocks & candidate_blocks)
    window_overlap = len(selected_windows & candidate_windows)
    selected_positions = [block_order[value] for value in selected_blocks]
    candidate_positions = [block_order[value] for value in candidate_blocks]
    distance = min(
        abs(left - right)
        for left in selected_positions
        for right in candidate_positions
    )
    first_window = min(
        window_order.get(value, 10**9) for value in candidate["window_ids"]
    )
    return (
        -block_overlap,
        -window_overlap,
        distance,
        first_window,
        str(candidate["candidate_id"]),
    )


def _packet_candidate_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"],
        "normalized_surface": row["normalized_surface"],
        "surfaces": row["surfaces"],
        "source_block_ids": row["source_block_ids"],
        "window_ids": row["window_ids"],
        "evidence_block_ids": row["evidence_block_ids"],
        "evidence_complete": row["evidence_complete"],
        "support_block_count": row["support_block_count"],
        "window_count": row["window_count"],
    }


def _render_packet(
    candidate_ids: Sequence[str],
    *,
    by_id: Mapping[str, Mapping[str, Any]],
    candidate_index: Mapping[str, Any],
    block_text: Mapping[str, str],
    block_order: Mapping[str, int],
) -> dict[str, Any]:
    candidates = [_packet_candidate_projection(by_id[value]) for value in candidate_ids]
    evidence_ids = sorted(
        {value for row in candidates for value in row["evidence_block_ids"]},
        key=block_order.__getitem__,
    )
    packet_id = "b2pkt_" + canonical_sha256(
        {
            "candidate_index_sha256": candidate_index["candidate_index_sha256"],
            "candidate_ids": sorted(candidate_ids),
        }
    )[:24].lower()
    return {
        "packet_id": packet_id,
        "chapter_id": candidate_index["chapter_id"],
        "candidates": candidates,
        "source_blocks": [
            {"block_id": block_id, "text": block_text[block_id]}
            for block_id in evidence_ids
        ],
    }


def _prompt_tokens(packet: Mapping[str, Any]) -> int:
    return int(estimate_prompt_tokens(render_messages(packet), RESPONSE_FORMAT))


def _packet_fits(
    packet: Mapping[str, Any], prompt_tokens: int, *, caps: PacketCaps
) -> bool:
    return (
        len(packet["candidates"]) <= caps.max_candidates
        and len(packet["source_blocks"]) <= caps.max_unique_blocks
        and prompt_tokens <= caps.prompt_token_cap
    )


def _assert_packet_caps(
    packet: Mapping[str, Any],
    prompt_tokens: int,
    *,
    caps: PacketCaps,
) -> None:
    if not _packet_fits(packet, prompt_tokens, caps=caps):
        raise B2PacketPlanError(
            "A candidate cannot fit inside the configured packet caps"
        )


def _dry_summary(
    packet_rows: Sequence[Mapping[str, Any]],
    *,
    candidate_count: int,
    aggregate_row_count: int,
    grouped_row_count: int,
    partial_evidence_candidates: int,
) -> dict[str, Any]:
    candidate_counts = [int(row["candidate_count"]) for row in packet_rows]
    block_counts = [int(row["source_block_count"]) for row in packet_rows]
    prompt_tokens = [int(row["prompt_tokens_est"]) for row in packet_rows]
    return {
        "status": "completed",
        "caps_satisfied": True,
        "packets": len(packet_rows),
        "aggregate_rows": aggregate_row_count,
        "grouped_rows": grouped_row_count,
        "candidate_rows": candidate_count,
        "candidate_exact_cover": sum(candidate_counts),
        "partial_evidence_candidates": partial_evidence_candidates,
        "candidate_count_distribution": _distribution(candidate_counts),
        "source_block_count_distribution": _distribution(block_counts),
        "prompt_token_distribution": _distribution(prompt_tokens),
        "prompt_tokens_sum": sum(prompt_tokens),
        "completion_tokens_low_sum_est": sum(
            int(row["completion_tokens_low_est"]) for row in packet_rows
        ),
        "completion_tokens_high_sum_est": sum(
            int(row["completion_tokens_high_est"]) for row in packet_rows
        ),
        "evidence_references": sum(
            int(row["evidence_references"]) for row in packet_rows
        ),
        "source_block_renders_across_packets": sum(block_counts),
        "within_packet_block_reuse_saved": sum(
            int(row["block_reuse_saved"]) for row in packet_rows
        ),
        "no_api_called": True,
    }


def _distribution(values: Sequence[int]) -> dict[str, int]:
    if not values:
        return {"min": 0, "p50": 0, "p95": 0, "max": 0}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p50": _nearest_rank(ordered, 50),
        "p95": _nearest_rank(ordered, 95),
        "max": ordered[-1],
    }


def _nearest_rank(ordered: Sequence[int], percentile: int) -> int:
    rank = max(1, (percentile * len(ordered) + 99) // 100)
    return int(ordered[min(rank - 1, len(ordered) - 1)])


def _write_or_verify_json(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise B2PacketPlanError(f"Refusing to overwrite changed artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic 0-API D2L B2 packet plan."
    )
    parser.add_argument("--aggregate", required=True)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument(
        "--max-unique-blocks", type=int, default=DEFAULT_MAX_UNIQUE_BLOCKS
    )
    parser.add_argument(
        "--prompt-token-cap", type=int, default=DEFAULT_PROMPT_TOKEN_CAP
    )
    parser.add_argument(
        "--max-evidence-blocks",
        type=int,
        default=DEFAULT_MAX_EVIDENCE_BLOCKS,
    )
    args = parser.parse_args()
    dry = plan_from_paths(
        aggregate_path=Path(args.aggregate),
        source_manifest_path=Path(args.source_manifest),
        out_dir=Path(args.out),
        caps=PacketCaps(
            max_candidates=args.max_candidates,
            max_unique_blocks=args.max_unique_blocks,
            prompt_token_cap=args.prompt_token_cap,
            max_evidence_blocks_per_candidate=args.max_evidence_blocks,
        ),
    )
    print(json.dumps(dry["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
