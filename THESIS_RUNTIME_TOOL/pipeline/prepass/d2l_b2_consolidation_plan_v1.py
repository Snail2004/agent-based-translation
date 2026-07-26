from __future__ import annotations

import argparse
from copy import deepcopy
import json
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.prepass.d2l_b2_consolidation_contract_v1 import (
    RESPONSE_FORMAT,
    ConsolidationValidation,
    prompt_sha256,
    render_messages,
    response_schema_sha256,
    user_payload_sha256,
    validate_output as validate_consolidation_output,
)
from pipeline.prepass.d2l_b2_packet_contract_v2 import (
    render_messages as render_b2_messages,
    user_payload_sha256 as b2_user_payload_sha256,
    validate_output as validate_b2_output,
)


PLAN_VERSION = "d2l_b2_consolidation_plan_v1"
INDEX_VERSION = "d2l_b2_consolidation_index_v1"
DRAFT_VERSION = "d2l_b2_consolidation_draft_v1"
DRY_RENDER_VERSION = "d2l_b2_consolidation_dry_render_v1"


class ConsolidationPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceSource:
    manifest_path: Path
    runtime_root: Path


@dataclass(frozen=True)
class ConsolidationCaps:
    max_components: int = 6
    max_members: int = 16
    max_unique_blocks: int = 24
    prompt_token_cap: int = 6000

    def validate(self) -> None:
        values = {
            "max_components": self.max_components,
            "max_members": self.max_members,
            "max_unique_blocks": self.max_unique_blocks,
            "prompt_token_cap": self.prompt_token_cap,
        }
        for name, value in values.items():
            if not isinstance(value, int) or value <= 0:
                raise ConsolidationPlanError(f"{name} must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        self.validate()
        return {
            "max_components": self.max_components,
            "max_members": self.max_members,
            "max_unique_blocks": self.max_unique_blocks,
            "prompt_token_cap": self.prompt_token_cap,
        }


def load_b2_evidence(
    *,
    sources: Sequence[EvidenceSource],
    requests_root: Path,
    expected_count: int | None = None,
) -> dict[str, Any]:
    if not sources:
        raise ConsolidationPlanError("At least one evidence source is required")
    if not requests_root.is_dir():
        raise ConsolidationPlanError(f"Request root does not exist: {requests_root}")

    rows: dict[str, dict[str, Any]] = {}
    blocks: dict[str, str] = {}
    lineage: list[dict[str, Any]] = []
    seen_packets: set[str] = set()

    for source in sources:
        manifest = _read_json(source.manifest_path)
        _verify_sealed_object(manifest, "manifest_sha256", source.manifest_path)
        manifest_sha = str(manifest["manifest_sha256"])
        packet_rows = manifest.get("packets")
        if not isinstance(packet_rows, list) or not packet_rows:
            raise ConsolidationPlanError(
                f"Manifest has no packet rows: {source.manifest_path}"
            )
        by_packet = {
            str(row.get("packet_id")): row
            for row in packet_rows
            if isinstance(row, dict) and isinstance(row.get("packet_id"), str)
        }
        if len(by_packet) != len(packet_rows):
            raise ConsolidationPlanError(
                f"Manifest packet IDs are invalid: {source.manifest_path}"
            )
        if not source.runtime_root.is_dir():
            raise ConsolidationPlanError(
                f"Runtime root does not exist: {source.runtime_root}"
            )

        valid_packet_ids: list[str] = []
        for validation_path in sorted(source.runtime_root.glob("*/validation.json")):
            raw_validation = _read_json(validation_path)
            if raw_validation.get("status") != "valid":
                continue
            packet_id = raw_validation.get("packet_id")
            if not isinstance(packet_id, str) or packet_id not in by_packet:
                raise ConsolidationPlanError(
                    f"Validation references an unknown packet: {validation_path}"
                )
            if packet_id in seen_packets:
                raise ConsolidationPlanError(
                    f"Packet is valid in more than one evidence source: {packet_id}"
                )
            seen_packets.add(packet_id)
            valid_packet_ids.append(packet_id)
            manifest_row = by_packet[packet_id]
            request_path = requests_root / packet_id / "request.json"
            request_bytes = request_path.read_bytes()
            if _sha256_bytes(request_bytes) != manifest_row.get(
                "source_request_sha256"
            ):
                raise ConsolidationPlanError(
                    f"Source request hash mismatch for packet {packet_id}"
                )
            request = json.loads(request_bytes.decode("utf-8"))
            if set(request) != {"messages", "packet", "summary"}:
                raise ConsolidationPlanError(
                    f"Source request has an invalid shape: {request_path}"
                )
            packet = request["packet"]
            messages = render_b2_messages(packet)
            if request["messages"] != messages:
                raise ConsolidationPlanError(
                    f"Stored messages do not re-render for packet {packet_id}"
                )
            payload_sha = b2_user_payload_sha256(messages)
            if payload_sha != manifest_row.get("user_payload_sha256"):
                raise ConsolidationPlanError(
                    f"Manifest user payload hash mismatch for packet {packet_id}"
                )
            if request["summary"].get("user_payload_sha256") != payload_sha:
                raise ConsolidationPlanError(
                    f"Request summary payload hash mismatch for packet {packet_id}"
                )
            _verify_manifest_packet_row(manifest_row, packet)

            parsed = {
                "packet_id": packet_id,
                "decisions": raw_validation.get("decisions"),
            }
            validation = validate_b2_output(parsed, packet=packet)
            if validation.errors:
                raise ConsolidationPlanError(
                    f"B2 validation no longer conforms for {packet_id}: "
                    + "; ".join(validation.errors)
                )
            if raw_validation.get("errors") != []:
                raise ConsolidationPlanError(
                    f"Stored validation has errors for packet {packet_id}"
                )
            if raw_validation.get("missing_candidate_ids") != [] or raw_validation.get(
                "duplicate_candidate_ids"
            ) != []:
                raise ConsolidationPlanError(
                    f"Stored validation is not exact-cover for packet {packet_id}"
                )

            candidates = {
                str(row["candidate_id"]): row for row in packet["candidates"]
            }
            for decision in validation.decisions:
                if decision.candidate_id in rows:
                    raise ConsolidationPlanError(
                        f"Candidate has duplicate valid decisions: {decision.candidate_id}"
                    )
                candidate = candidates[decision.candidate_id]
                rows[decision.candidate_id] = _evidence_row(
                    candidate=candidate,
                    decision=decision,
                    packet_id=packet_id,
                    manifest_sha256=manifest_sha,
                    request_sha256=str(manifest_row["source_request_sha256"]),
                    validation_sha256=_sha256_path(validation_path),
                    chapter_id=str(packet["chapter_id"]),
                )
            for block in packet["source_blocks"]:
                block_id = str(block["block_id"])
                text = str(block["text"])
                previous = blocks.setdefault(block_id, text)
                if previous != text:
                    raise ConsolidationPlanError(
                        f"Source block bytes conflict for {block_id}"
                    )

        lineage.append(
            {
                "manifest_sha256": manifest_sha,
                "valid_packet_ids": sorted(valid_packet_ids),
            }
        )

    if expected_count is not None and len(rows) != expected_count:
        raise ConsolidationPlanError(
            f"Expected {expected_count} decisions, loaded {len(rows)}"
        )
    if not rows:
        raise ConsolidationPlanError("No valid B2 decisions were loaded")

    chapter_ids = sorted({str(row["chapter_id"]) for row in rows.values()})
    payload = {
        "index_version": INDEX_VERSION,
        "chapter_ids": chapter_ids,
        "source_lineage": sorted(
            lineage, key=lambda row: row["manifest_sha256"]
        ),
        "decisions": sorted(rows.values(), key=_candidate_sort_key),
        "source_blocks": [
            {"block_id": block_id, "text": blocks[block_id]}
            for block_id in sorted(blocks, key=_text_sort_key)
        ],
    }
    payload["counts"] = _index_counts(payload["decisions"])
    payload["index_sha256"] = _sha256_json(payload)
    return payload


def build_component_plan(index: Mapping[str, Any]) -> dict[str, Any]:
    _verify_sealed_mapping(index, "index_sha256", "consolidation index")
    decisions = index.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ConsolidationPlanError("Consolidation index has no decisions")
    admitted = [row for row in decisions if row.get("decision") == "admit"]
    review = [row for row in decisions if row.get("decision") == "review"]
    rejected = [row for row in decisions if row.get("decision") == "reject"]
    admitted_by_id = {str(row["candidate_id"]): row for row in admitted}

    adjacency: dict[str, set[str]] = {candidate_id: set() for candidate_id in admitted_by_id}
    edges: list[dict[str, Any]] = []
    admitted_ordered = sorted(admitted, key=_candidate_sort_key)
    for left_index, left in enumerate(admitted_ordered):
        for right in admitted_ordered[left_index + 1 :]:
            signals = _pair_signals(left, right)
            if not signals:
                continue
            left_id = str(left["candidate_id"])
            right_id = str(right["candidate_id"])
            adjacency[left_id].add(right_id)
            adjacency[right_id].add(left_id)
            edges.append(
                {
                    "left_candidate_id": left_id,
                    "right_candidate_id": right_id,
                    "signals": signals,
                }
            )

    connected = _connected_components(adjacency)
    edge_lookup = {
        frozenset((row["left_candidate_id"], row["right_candidate_id"])): row
        for row in edges
    }
    components: list[dict[str, Any]] = []
    assigned: set[str] = set()
    for member_ids in connected:
        if len(member_ids) == 1:
            candidate = admitted_by_id[member_ids[0]]
            if len(candidate["target_proposals"]) <= 1:
                continue
            component_edges: list[dict[str, Any]] = []
            reasons = ["multiple_targets"]
        else:
            component_edges = []
            reasons_set: set[str] = set()
            for left_index, left_id in enumerate(member_ids):
                for right_id in member_ids[left_index + 1 :]:
                    edge = edge_lookup.get(frozenset((left_id, right_id)))
                    if edge is not None:
                        component_edges.append(dict(edge))
                        reasons_set.update(edge["signals"])
            reasons = sorted(reasons_set)
            if any(
                len(admitted_by_id[candidate_id]["target_proposals"]) > 1
                for candidate_id in member_ids
            ):
                reasons.append("multiple_targets")
                reasons = sorted(set(reasons))
        members = [
            _to_component_member(admitted_by_id[candidate_id])
            for candidate_id in member_ids
        ]
        chapter_ids = {
            str(admitted_by_id[candidate_id]["chapter_id"])
            for candidate_id in member_ids
        }
        if len(chapter_ids) != 1:
            raise ConsolidationPlanError("A component crosses chapter boundaries")
        source_ids = _stable_unique(
            block_id
            for member in members
            for block_id in member["evidence_block_ids"]
        )
        component_identity = {
            "index_sha256": index["index_sha256"],
            "member_candidate_ids": sorted(member_ids),
            "reason_codes": reasons,
            "edges": sorted(component_edges, key=_edge_sort_key),
        }
        component_id = "cmp_" + _sha256_json(component_identity)[:24].lower()
        components.append(
            {
                "component_id": component_id,
                "chapter_id": next(iter(chapter_ids)),
                "reason_codes": reasons,
                "members": sorted(members, key=_candidate_sort_key),
                "edges": sorted(component_edges, key=_edge_sort_key),
                "source_block_ids": sorted(source_ids, key=_text_sort_key),
            }
        )
        assigned.update(member_ids)

    provisional = [
        _to_provisional_clean(row)
        for row in admitted_ordered
        if row["candidate_id"] not in assigned
    ]
    component_member_ids = {
        member["candidate_id"]
        for component in components
        for member in component["members"]
    }
    if component_member_ids != assigned or component_member_ids & {
        row["candidate_id"] for row in provisional
    }:
        raise ConsolidationPlanError("Admitted candidate assignment is inconsistent")
    if component_member_ids | {
        row["candidate_id"] for row in provisional
    } != set(admitted_by_id):
        raise ConsolidationPlanError("Admitted candidates are not exact-covered")

    plan = {
        "plan_version": PLAN_VERSION,
        "source_index_sha256": index["index_sha256"],
        "components": sorted(components, key=_component_sort_key),
        "provisional_clean": sorted(provisional, key=_candidate_sort_key),
        "pending_admission": sorted(
            [_to_nonadmitted_ledger(row) for row in review],
            key=_candidate_sort_key,
        ),
        "rejected_ledger": sorted(
            [_to_nonadmitted_ledger(row) for row in rejected],
            key=_candidate_sort_key,
        ),
    }
    plan["counts"] = {
        "components": len(plan["components"]),
        "component_members": len(component_member_ids),
        "provisional_clean": len(plan["provisional_clean"]),
        "pending_admission": len(plan["pending_admission"]),
        "rejected": len(plan["rejected_ledger"]),
        "exact_cover": (
            len(component_member_ids)
            + len(plan["provisional_clean"])
            + len(plan["pending_admission"])
            + len(plan["rejected_ledger"])
        ),
    }
    plan["plan_sha256"] = _sha256_json(plan)
    return plan


def partition_oversized_components(
    *,
    plan: Mapping[str, Any],
    index: Mapping[str, Any],
    caps: ConsolidationCaps,
    min_excerpt_chars: int,
) -> dict[str, Any]:
    """Split only atomic components that cannot fit the sealed request caps."""

    caps.validate()
    if not isinstance(min_excerpt_chars, int) or min_excerpt_chars < 128:
        raise ConsolidationPlanError("min_excerpt_chars must be at least 128")
    _verify_sealed_mapping(plan, "plan_sha256", "component plan")
    _verify_sealed_mapping(index, "index_sha256", "consolidation index")
    if plan.get("source_index_sha256") != index.get("index_sha256"):
        raise ConsolidationPlanError("Plan and index lineage do not match")

    block_map = {
        str(row["block_id"]): str(row["text"]) for row in index["source_blocks"]
    }
    projected: list[dict[str, Any]] = []
    for component in plan["components"]:
        if _component_fits_caps(
            component=component,
            block_map=block_map,
            caps=caps,
            min_excerpt_chars=min_excerpt_chars,
        ):
            projected.append(deepcopy(component))
            continue
        projected.extend(
            _partition_atomic_component(
                component=component,
                block_map=block_map,
                caps=caps,
                min_excerpt_chars=min_excerpt_chars,
            )
        )

    expected_members = {
        str(member["candidate_id"])
        for component in plan["components"]
        for member in component["members"]
    }
    actual_members = [
        str(member["candidate_id"])
        for component in projected
        for member in component["members"]
    ]
    if len(actual_members) != len(set(actual_members)):
        raise ConsolidationPlanError(
            "Partitioned components contain duplicate candidates"
        )
    if set(actual_members) != expected_members:
        raise ConsolidationPlanError(
            "Partitioned components do not exact-cover source component members"
        )

    payload = deepcopy(dict(plan))
    payload.pop("plan_sha256", None)
    payload["components"] = sorted(projected, key=_component_sort_key)
    payload["counts"] = dict(payload["counts"])
    payload["counts"]["components"] = len(payload["components"])
    payload["counts"]["component_members"] = len(actual_members)
    payload["plan_sha256"] = _sha256_json(payload)
    return payload


def packetize_components(
    *,
    plan: Mapping[str, Any],
    index: Mapping[str, Any],
    caps: ConsolidationCaps,
    oversized_component_min_excerpt_chars: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    caps.validate()
    if (
        oversized_component_min_excerpt_chars is not None
        and (
            not isinstance(oversized_component_min_excerpt_chars, int)
            or oversized_component_min_excerpt_chars < 128
        )
    ):
        raise ConsolidationPlanError(
            "oversized_component_min_excerpt_chars must be at least 128"
        )
    _verify_sealed_mapping(plan, "plan_sha256", "component plan")
    _verify_sealed_mapping(index, "index_sha256", "consolidation index")
    if plan.get("source_index_sha256") != index.get("index_sha256"):
        raise ConsolidationPlanError("Plan and index lineage do not match")
    block_map = {
        str(row["block_id"]): str(row["text"]) for row in index["source_blocks"]
    }
    components = [dict(row) for row in plan["components"]]
    packets: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for component in components:
        trial = current + [component]
        packet = _make_packet(trial, block_map)
        prompt_tokens = estimate_prompt_tokens(
            render_messages(packet), RESPONSE_FORMAT
        )
        if _packet_fits(packet, prompt_tokens, caps):
            current = trial
            continue
        if not current:
            compact = _fit_oversized_component_packet(
                component=component,
                block_map=block_map,
                caps=caps,
                min_excerpt_chars=oversized_component_min_excerpt_chars,
            )
            if compact is not None:
                packets.append(compact)
                continue
            raise ConsolidationPlanError(
                f"Component cannot fit packet caps: {component['component_id']}"
            )
        packets.append(_make_packet(current, block_map))
        current = [component]
        single = _make_packet(current, block_map)
        single_tokens = estimate_prompt_tokens(
            render_messages(single), RESPONSE_FORMAT
        )
        if not _packet_fits(single, single_tokens, caps):
            compact = _fit_oversized_component_packet(
                component=component,
                block_map=block_map,
                caps=caps,
                min_excerpt_chars=oversized_component_min_excerpt_chars,
            )
            if compact is not None:
                packets.append(compact)
                current = []
                continue
            raise ConsolidationPlanError(
                f"Component cannot fit packet caps: {component['component_id']}"
            )
    if current:
        packets.append(_make_packet(current, block_map))

    summaries: list[dict[str, Any]] = []
    seen_components: set[str] = set()
    seen_members: set[str] = set()
    for packet in packets:
        messages = render_messages(packet)
        prompt_tokens = estimate_prompt_tokens(messages, RESPONSE_FORMAT)
        if not _packet_fits(packet, prompt_tokens, caps):
            raise ConsolidationPlanError(
                f"Final packet violates caps: {packet['packet_id']}"
            )
        component_ids = [row["component_id"] for row in packet["components"]]
        member_ids = [
            member["candidate_id"]
            for component in packet["components"]
            for member in component["members"]
        ]
        if seen_components.intersection(component_ids):
            raise ConsolidationPlanError("Component appears in multiple packets")
        if seen_members.intersection(member_ids):
            raise ConsolidationPlanError("Candidate appears in multiple packets")
        seen_components.update(component_ids)
        seen_members.update(member_ids)
        summaries.append(
            {
                "packet_id": packet["packet_id"],
                "chapter_id": packet["chapter_id"],
                "component_ids": component_ids,
                "component_count": len(component_ids),
                "member_candidate_ids": member_ids,
                "member_count": len(member_ids),
                "source_block_ids": [
                    row["block_id"] for row in packet["source_blocks"]
                ],
                "source_block_count": len(packet["source_blocks"]),
                "prompt_tokens_est": prompt_tokens,
                "user_payload_sha256": user_payload_sha256(messages),
            }
        )

    expected_components = {row["component_id"] for row in components}
    if seen_components != expected_components:
        raise ConsolidationPlanError("Packets do not exact-cover components")
    dry = {
        "dry_render_version": DRY_RENDER_VERSION,
        "no_api_called": True,
        "source_index_sha256": index["index_sha256"],
        "source_plan_sha256": plan["plan_sha256"],
        "prompt_sha256": prompt_sha256(),
        "response_schema_sha256": response_schema_sha256(),
        "caps": caps.to_dict(),
        "packets": summaries,
        "totals": {
            "packet_count": len(packets),
            "component_count": len(seen_components),
            "member_count": len(seen_members),
            "source_block_renders": sum(
                row["source_block_count"] for row in summaries
            ),
            "prompt_tokens_est": sum(row["prompt_tokens_est"] for row in summaries),
        },
    }
    dry["dry_render_sha256"] = _sha256_json(dry)
    return packets, dry


def write_plan_artifacts(
    *,
    out_dir: Path,
    index: Mapping[str, Any],
    plan: Mapping[str, Any],
    packets: Sequence[Mapping[str, Any]],
    dry_render: Mapping[str, Any],
) -> None:
    _write_or_verify_json(out_dir / "consolidation_index.json", index)
    _write_or_verify_json(out_dir / "component_plan.json", plan)
    summaries = {
        str(row["packet_id"]): row for row in dry_render["packets"]
    }
    for packet in packets:
        messages = render_messages(packet)
        summary = summaries[str(packet["packet_id"])]
        request = {
            "messages": messages,
            "packet": packet,
            "response_format": RESPONSE_FORMAT,
            "summary": summary,
        }
        _write_or_verify_json(
            out_dir / "packets" / str(packet["packet_id"]) / "request.json",
            request,
        )
    _write_or_verify_json(out_dir / "dry_render.json", dry_render)


def build_draft_package(
    *,
    index: Mapping[str, Any],
    plan: Mapping[str, Any],
    packet_validations: Sequence[
        tuple[Mapping[str, Any], ConsolidationValidation]
    ],
) -> dict[str, Any]:
    _verify_sealed_mapping(index, "index_sha256", "consolidation index")
    _verify_sealed_mapping(plan, "plan_sha256", "component plan")
    if plan.get("source_index_sha256") != index.get("index_sha256"):
        raise ConsolidationPlanError("Draft inputs have mismatched lineage")

    components = {
        str(row["component_id"]): row for row in plan["components"]
    }
    seen_components: set[str] = set()
    audited_entries: list[dict[str, Any]] = []
    pending_components: list[dict[str, Any]] = []
    for packet, validation in packet_validations:
        if validation.errors:
            raise ConsolidationPlanError(
                "Cannot apply an invalid consolidation response: "
                + "; ".join(validation.errors)
            )
        packet_components = {
            str(row["component_id"]): row for row in packet["components"]
        }
        expected = set(packet_components)
        decided = {row.component_id for row in validation.decisions}
        if expected != decided:
            raise ConsolidationPlanError(
                "Validated response does not exact-cover packet components"
            )
        for decision in validation.decisions:
            if decision.component_id in seen_components:
                raise ConsolidationPlanError(
                    f"Component applied more than once: {decision.component_id}"
                )
            seen_components.add(decision.component_id)
            component = components.get(decision.component_id)
            projected_component = (
                {
                    key: value
                    for key, value in component.items()
                    if key != "chapter_id"
                }
                if component is not None
                else None
            )
            if (
                component is None
                or packet.get("chapter_id") != component.get("chapter_id")
                or projected_component != packet_components[decision.component_id]
            ):
                raise ConsolidationPlanError(
                    f"Packet component does not match plan: {decision.component_id}"
                )
            if decision.action == "pending":
                pending_components.append(
                    {
                        "component_id": decision.component_id,
                        "member_candidate_ids": [
                            row["candidate_id"] for row in component["members"]
                        ],
                        "pending_reason": decision.pending_reason,
                        "source_block_ids": component["source_block_ids"],
                    }
                )
                continue
            member_map = {
                str(row["candidate_id"]): row for row in component["members"]
            }
            for resolved in decision.resolved_entries:
                member_ids = sorted(
                    resolved.member_candidate_ids, key=_text_sort_key
                )
                members = [member_map[candidate_id] for candidate_id in member_ids]
                all_evidence = sorted(
                    _stable_unique(
                        block_id
                        for member in members
                        for block_id in member["evidence_block_ids"]
                    ),
                    key=_text_sort_key,
                )
                other_surfaces = sorted(
                    {
                        surface
                        for member in members
                        for surface in member["surfaces"]
                        if surface != resolved.canonical_source
                    },
                    key=_text_sort_key,
                )
                source_variants = [resolved.canonical_source] + other_surfaces
                entry_identity = {
                    "source_index_sha256": index["index_sha256"],
                    "component_id": decision.component_id,
                    "member_candidate_ids": member_ids,
                    "canonical_source": resolved.canonical_source,
                    "canonical_target_vi": resolved.canonical_target_vi,
                    "directive": resolved.directive,
                }
                audited_entries.append(
                    {
                        "draft_entry_id": "d2lte_"
                        + _sha256_json(entry_identity)[:24].lower(),
                        "status": "audited_draft",
                        "component_id": decision.component_id,
                        "member_candidate_ids": member_ids,
                        "canonical_source": resolved.canonical_source,
                        "source_variants": source_variants,
                        "canonical_target_vi": resolved.canonical_target_vi,
                        "alternative_targets": [
                            {
                                "target_vi": row.target_vi,
                                "applicability": row.applicability,
                            }
                            for row in resolved.alternative_targets
                        ],
                        "directive": resolved.directive,
                        "evidence_block_ids": all_evidence,
                        "auditor_cited_evidence_block_ids": list(
                            sorted(resolved.evidence_block_ids, key=_text_sort_key)
                        ),
                        "rationale": resolved.rationale,
                    }
                )

    expected_components = set(components)
    if seen_components != expected_components:
        missing = sorted(expected_components - seen_components)
        raise ConsolidationPlanError(
            "Draft is missing component decisions: " + ", ".join(missing)
        )

    draft = {
        "draft_version": DRAFT_VERSION,
        "production_published": False,
        "source_index_sha256": index["index_sha256"],
        "source_plan_sha256": plan["plan_sha256"],
        "audited_entries": sorted(
            audited_entries, key=lambda row: _text_sort_key(row["draft_entry_id"])
        ),
        "provisional_clean": plan["provisional_clean"],
        "pending_components": sorted(
            pending_components, key=lambda row: _text_sort_key(row["component_id"])
        ),
        "pending_admission": plan["pending_admission"],
        "rejected_ledger": plan["rejected_ledger"],
    }
    draft["counts"] = {
        "audited_entries": len(draft["audited_entries"]),
        "provisional_clean": len(draft["provisional_clean"]),
        "pending_components": len(draft["pending_components"]),
        "pending_admission": len(draft["pending_admission"]),
        "rejected": len(draft["rejected_ledger"]),
    }
    draft["draft_sha256"] = _sha256_json(draft)
    return draft


def _evidence_row(
    *,
    candidate: Mapping[str, Any],
    decision: Any,
    packet_id: str,
    manifest_sha256: str,
    request_sha256: str,
    validation_sha256: str,
    chapter_id: str,
) -> dict[str, Any]:
    return {
        "candidate_id": decision.candidate_id,
        "chapter_id": chapter_id,
        "surfaces": list(candidate["surfaces"]),
        "decision": decision.decision,
        "canonical_source": decision.canonical_source,
        "target_proposals": [
            {
                "target_vi": row.target_vi,
                "applicability": row.applicability,
            }
            for row in decision.target_proposals
        ],
        "directive": decision.directive,
        "evidence_block_ids": list(decision.evidence_block_ids),
        "evidence_complete": bool(candidate["evidence_complete"]),
        "decision_rationale": decision.rationale,
        "lineage": {
            "packet_id": packet_id,
            "manifest_sha256": manifest_sha256,
            "source_request_sha256": request_sha256,
            "validation_sha256": validation_sha256,
        },
    }


def _verify_manifest_packet_row(
    row: Mapping[str, Any], packet: Mapping[str, Any]
) -> None:
    candidate_ids = [str(candidate["candidate_id"]) for candidate in packet["candidates"]]
    if row.get("candidate_ids") != candidate_ids:
        raise ConsolidationPlanError(
            f"Manifest candidate IDs do not match packet {packet['packet_id']}"
        )
    if row.get("candidate_count") != len(candidate_ids):
        raise ConsolidationPlanError(
            f"Manifest candidate count does not match packet {packet['packet_id']}"
        )
    if row.get("source_block_count") != len(packet["source_blocks"]):
        raise ConsolidationPlanError(
            f"Manifest block count does not match packet {packet['packet_id']}"
        )


def _pair_signals(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    signals: set[str] = set()
    left_targets = {
        _normalize_text(row["target_vi"]) for row in left["target_proposals"]
    }
    right_targets = {
        _normalize_text(row["target_vi"]) for row in right["target_proposals"]
    }
    if left_targets & right_targets:
        signals.add("shared_target")

    left_sources = _source_forms(left)
    right_sources = _source_forms(right)
    if any(
        _is_source_form_variant(a, b) for a in left_sources for b in right_sources
    ):
        signals.add("source_form_variant")
    if any(
        _is_token_containment(a, b) for a in left_sources for b in right_sources
    ):
        signals.add("source_token_containment")
    return sorted(signals)


def _source_forms(row: Mapping[str, Any]) -> list[str]:
    values = list(row.get("surfaces") or [])
    canonical = row.get("canonical_source")
    if isinstance(canonical, str):
        values.append(canonical)
    return _stable_unique(value for value in values if isinstance(value, str))


def _is_source_form_variant(left: str, right: str) -> bool:
    left_tokens = _source_tokens(left)
    right_tokens = _source_tokens(right)
    if not left_tokens or not right_tokens or len(left_tokens) != len(right_tokens):
        return False
    if left_tokens[:-1] != right_tokens[:-1]:
        return False
    a = left_tokens[-1]
    b = right_tokens[-1]
    if a == b:
        return False
    return _inflection_stems(a) & _inflection_stems(b) != set()


def _inflection_stems(token: str) -> set[str]:
    stems = {token}
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        stems.add(token[:-1])
    if len(token) > 4 and token.endswith("ies"):
        stems.add(token[:-3] + "y")
    if len(token) > 3 and token.endswith("es"):
        stems.add(token[:-2])
    if len(token) > 3 and token.endswith("is"):
        stems.add(token[:-2])
    return stems


def _is_token_containment(left: str, right: str) -> bool:
    left_tokens = _source_tokens(left)
    right_tokens = _source_tokens(right)
    if not left_tokens or not right_tokens or left_tokens == right_tokens:
        return False
    if len(left_tokens) > len(right_tokens):
        left_tokens, right_tokens = right_tokens, left_tokens
    if len(right_tokens) - len(left_tokens) != 1:
        return False
    width = len(left_tokens)
    return any(
        right_tokens[index : index + width] == left_tokens
        for index in range(len(right_tokens) - width + 1)
    )


def _source_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return [token for token in re.findall(r"[^\W_]+", normalized) if token]


def _connected_components(adjacency: Mapping[str, set[str]]) -> list[list[str]]:
    remaining = set(adjacency)
    components: list[list[str]] = []
    while remaining:
        root = min(remaining, key=_text_sort_key)
        stack = [root]
        members: set[str] = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            stack.extend(sorted(adjacency[current] - members, key=_text_sort_key))
        remaining -= members
        components.append(sorted(members, key=_text_sort_key))
    return sorted(components, key=lambda values: tuple(_text_sort_key(v) for v in values))


def _to_component_member(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"],
        "canonical_source": row["canonical_source"],
        "surfaces": row["surfaces"],
        "target_proposals": row["target_proposals"],
        "directive": row["directive"],
        "evidence_block_ids": row["evidence_block_ids"],
        "evidence_complete": row["evidence_complete"],
        "decision_rationale": row["decision_rationale"],
    }


def _to_provisional_clean(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"],
        "status": "provisional_clean",
        "chapter_id": row["chapter_id"],
        "canonical_source": row["canonical_source"],
        "surfaces": row["surfaces"],
        "target_proposals": row["target_proposals"],
        "directive": row["directive"],
        "evidence_block_ids": row["evidence_block_ids"],
        "lineage": row["lineage"],
    }


def _to_nonadmitted_ledger(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"],
        "status": "pending_admission"
        if row["decision"] == "review"
        else "rejected",
        "chapter_id": row["chapter_id"],
        "surfaces": row["surfaces"],
        "evidence_block_ids": row["evidence_block_ids"],
        "decision_rationale": row["decision_rationale"],
        "lineage": row["lineage"],
    }


def _make_packet(
    components: Sequence[Mapping[str, Any]],
    block_map: Mapping[str, str],
    *,
    source_text_limit: int | None = None,
) -> dict[str, Any]:
    if not components:
        raise ConsolidationPlanError("Cannot create an empty packet")
    chapter_ids = {str(row["chapter_id"]) for row in components}
    if len(chapter_ids) != 1:
        raise ConsolidationPlanError("A packet cannot cross chapter boundaries")
    packet_components = [
        {
            key: value
            for key, value in component.items()
            if key != "chapter_id"
        }
        for component in components
    ]
    block_ids = sorted(
        _stable_unique(
            block_id
            for component in components
            for block_id in component["source_block_ids"]
        ),
        key=_text_sort_key,
    )
    missing = [block_id for block_id in block_ids if block_id not in block_map]
    if missing:
        raise ConsolidationPlanError(
            "Component cites unavailable blocks: " + ", ".join(missing)
        )
    block_surfaces: dict[str, list[str]] = {block_id: [] for block_id in block_ids}
    for component in components:
        for member in component["members"]:
            surfaces = [
                str(value)
                for value in member["surfaces"]
                if isinstance(value, str) and value
            ]
            for block_id in member["evidence_block_ids"]:
                if block_id in block_surfaces:
                    block_surfaces[block_id].extend(surfaces)
    projected_blocks = [
        {
            "block_id": block_id,
            "text": (
                _evidence_excerpt(
                    block_map[block_id],
                    surfaces=block_surfaces[block_id],
                    limit=source_text_limit,
                )
                if source_text_limit is not None
                else block_map[block_id]
            ),
        }
        for block_id in block_ids
    ]
    identity = {
        "plan_version": PLAN_VERSION,
        "chapter_id": next(iter(chapter_ids)),
        "component_ids": [row["component_id"] for row in packet_components],
        "source_block_ids": block_ids,
        "prompt_sha256": prompt_sha256(),
        "response_schema_sha256": response_schema_sha256(),
    }
    if source_text_limit is not None:
        identity["source_text_projection_sha256"] = _sha256_json(projected_blocks)
    return {
        "packet_id": "conpkt_" + _sha256_json(identity)[:24].lower(),
        "chapter_id": next(iter(chapter_ids)),
        "components": packet_components,
        "source_blocks": projected_blocks,
    }


def _fit_oversized_component_packet(
    *,
    component: Mapping[str, Any],
    block_map: Mapping[str, str],
    caps: ConsolidationCaps,
    min_excerpt_chars: int | None,
) -> dict[str, Any] | None:
    if min_excerpt_chars is None:
        return None
    original = _make_packet([component], block_map)
    if (
        len(original["components"]) > caps.max_components
        or len(component["members"]) > caps.max_members
        or len(original["source_blocks"]) > caps.max_unique_blocks
    ):
        return None
    maximum = max(len(str(row["text"])) for row in original["source_blocks"])
    if maximum <= min_excerpt_chars:
        return None

    lower = min_excerpt_chars
    upper = maximum
    best: dict[str, Any] | None = None
    while lower <= upper:
        limit = (lower + upper) // 2
        packet = _make_packet(
            [component],
            block_map,
            source_text_limit=limit,
        )
        prompt_tokens = estimate_prompt_tokens(
            render_messages(packet), RESPONSE_FORMAT
        )
        if _packet_fits(packet, prompt_tokens, caps):
            best = packet
            lower = limit + 1
        else:
            upper = limit - 1
    return best


def _component_fits_caps(
    *,
    component: Mapping[str, Any],
    block_map: Mapping[str, str],
    caps: ConsolidationCaps,
    min_excerpt_chars: int,
) -> bool:
    packet = _make_packet([component], block_map)
    prompt_tokens = estimate_prompt_tokens(render_messages(packet), RESPONSE_FORMAT)
    if _packet_fits(packet, prompt_tokens, caps):
        return True
    return (
        _fit_oversized_component_packet(
            component=component,
            block_map=block_map,
            caps=caps,
            min_excerpt_chars=min_excerpt_chars,
        )
        is not None
    )


def _partition_atomic_component(
    *,
    component: Mapping[str, Any],
    block_map: Mapping[str, str],
    caps: ConsolidationCaps,
    min_excerpt_chars: int,
) -> list[dict[str, Any]]:
    members = {
        str(row["candidate_id"]): deepcopy(row) for row in component["members"]
    }
    groups: dict[str, set[str]] = {
        candidate_id: {candidate_id} for candidate_id in members
    }
    owner = {candidate_id: candidate_id for candidate_id in members}
    edges = [deepcopy(row) for row in component["edges"]]

    for edge in sorted(edges, key=_partition_edge_sort_key):
        left_id = str(edge["left_candidate_id"])
        right_id = str(edge["right_candidate_id"])
        left_owner = owner[left_id]
        right_owner = owner[right_id]
        if left_owner == right_owner:
            continue
        merged_ids = groups[left_owner] | groups[right_owner]
        merged = _derived_component(
            source_component=component,
            member_ids=merged_ids,
            members=members,
            edges=edges,
        )
        if not _component_fits_caps(
            component=merged,
            block_map=block_map,
            caps=caps,
            min_excerpt_chars=min_excerpt_chars,
        ):
            continue
        merged_owner = min(merged_ids, key=_text_sort_key)
        del groups[left_owner]
        del groups[right_owner]
        groups[merged_owner] = merged_ids
        for candidate_id in merged_ids:
            owner[candidate_id] = merged_owner

    result = [
        _derived_component(
            source_component=component,
            member_ids=member_ids,
            members=members,
            edges=edges,
        )
        for member_ids in groups.values()
    ]
    if len(result) <= 1:
        raise ConsolidationPlanError(
            f"Component cannot be safely partitioned: {component['component_id']}"
        )
    for row in result:
        if not _component_fits_caps(
            component=row,
            block_map=block_map,
            caps=caps,
            min_excerpt_chars=min_excerpt_chars,
        ):
            raise ConsolidationPlanError(
                f"Partitioned component cannot fit packet caps: {row['component_id']}"
            )
    return sorted(result, key=_component_sort_key)


def _derived_component(
    *,
    source_component: Mapping[str, Any],
    member_ids: set[str],
    members: Mapping[str, Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    selected_members = sorted(
        [deepcopy(members[candidate_id]) for candidate_id in member_ids],
        key=_candidate_sort_key,
    )
    selected_edges = sorted(
        [
            deepcopy(edge)
            for edge in edges
            if str(edge["left_candidate_id"]) in member_ids
            and str(edge["right_candidate_id"]) in member_ids
        ],
        key=_edge_sort_key,
    )
    reasons = {
        str(signal)
        for edge in selected_edges
        for signal in edge["signals"]
    }
    if any(len(member["target_proposals"]) > 1 for member in selected_members):
        reasons.add("multiple_targets")
    if not reasons:
        reasons.update(str(value) for value in source_component["reason_codes"])
    reason_codes = sorted(reasons, key=_text_sort_key)
    source_block_ids = sorted(
        _stable_unique(
            str(block_id)
            for member in selected_members
            for block_id in member["evidence_block_ids"]
        ),
        key=_text_sort_key,
    )
    identity = {
        "source_component_id": str(source_component["component_id"]),
        "member_candidate_ids": sorted(member_ids, key=_text_sort_key),
        "reason_codes": reason_codes,
        "edges": selected_edges,
    }
    return {
        "component_id": "cmp_" + _sha256_json(identity)[:24].lower(),
        "chapter_id": str(source_component["chapter_id"]),
        "reason_codes": reason_codes,
        "members": selected_members,
        "edges": selected_edges,
        "source_block_ids": source_block_ids,
    }


def _partition_edge_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    signals = {str(value) for value in row["signals"]}
    priority = (
        4 * int("shared_target" in signals)
        + 3 * int("source_form_variant" in signals)
        + int("source_token_containment" in signals)
    )
    return (-priority, *_edge_sort_key(row))


def _evidence_excerpt(
    text: str,
    *,
    surfaces: Sequence[str],
    limit: int,
) -> str:
    if len(text) <= limit:
        return text
    body_limit = max(1, limit - 6)
    folded = text.casefold()
    matches: list[tuple[int, int]] = []
    for surface in sorted(set(surfaces), key=lambda value: (len(value), value.casefold())):
        needle = surface.casefold()
        if not needle:
            continue
        start = folded.find(needle)
        if start >= 0:
            matches.append((start, start + len(surface)))
    if matches:
        anchor_start, anchor_end = min(matches)
        center = (anchor_start + anchor_end) // 2
        start = max(0, min(len(text) - body_limit, center - body_limit // 2))
    else:
        start = 0
    end = min(len(text), start + body_limit)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    excerpt = prefix + text[start:end] + suffix
    return excerpt[:limit]


def _packet_fits(
    packet: Mapping[str, Any], prompt_tokens: int, caps: ConsolidationCaps
) -> bool:
    components = packet["components"]
    member_count = sum(len(row["members"]) for row in components)
    return (
        len(components) <= caps.max_components
        and member_count <= caps.max_members
        and len(packet["source_blocks"]) <= caps.max_unique_blocks
        and prompt_tokens <= caps.prompt_token_cap
    )


def _index_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "total": len(rows),
        "admit": sum(row["decision"] == "admit" for row in rows),
        "review": sum(row["decision"] == "review" for row in rows),
        "reject": sum(row["decision"] == "reject" for row in rows),
    }


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _candidate_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    source = row.get("canonical_source")
    surfaces = row.get("surfaces") or []
    label = source if isinstance(source, str) else (surfaces[0] if surfaces else "")
    return (_normalize_text(str(label)), str(row.get("candidate_id") or ""))


def _component_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    blocks = row.get("source_block_ids") or []
    first = blocks[0] if blocks else ""
    return (_text_sort_key(str(first)), str(row.get("component_id") or ""))


def _edge_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (str(row["left_candidate_id"]), str(row["right_candidate_id"]))


def _text_sort_key(value: str) -> tuple[str, str]:
    return (_normalize_text(value), value)


def _stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsolidationPlanError(f"Cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConsolidationPlanError(f"JSON artifact must be an object: {path}")
    return value


def _verify_sealed_object(
    value: Mapping[str, Any], hash_key: str, path: Path
) -> None:
    stored = value.get(hash_key)
    unsigned = dict(value)
    unsigned.pop(hash_key, None)
    if not isinstance(stored, str) or stored != _sha256_json(unsigned):
        raise ConsolidationPlanError(f"Sealed hash mismatch: {path}")


def _verify_sealed_mapping(
    value: Mapping[str, Any], hash_key: str, label: str
) -> None:
    stored = value.get(hash_key)
    unsigned = dict(value)
    unsigned.pop(hash_key, None)
    if not isinstance(stored, str) or stored != _sha256_json(unsigned):
        raise ConsolidationPlanError(f"{label} hash mismatch")


def _sha256_json(value: Any) -> str:
    rendered = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(rendered.encode("utf-8")).hexdigest().upper()


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest().upper()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_or_verify_json(path: Path, value: Mapping[str, Any]) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise ConsolidationPlanError(
                f"Refusing to overwrite changed artifact: {path}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic 0-API D2L B2 consolidation plan."
    )
    parser.add_argument("--manifest", action="append", required=True)
    parser.add_argument("--runtime-root", action="append", required=True)
    parser.add_argument("--requests-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--expected-count", type=int, default=42)
    parser.add_argument("--max-components", type=int, default=6)
    parser.add_argument("--max-members", type=int, default=16)
    parser.add_argument("--max-unique-blocks", type=int, default=24)
    parser.add_argument("--prompt-token-cap", type=int, default=6000)
    args = parser.parse_args(argv)
    if len(args.manifest) != len(args.runtime_root):
        parser.error("--manifest and --runtime-root counts must match")
    sources = [
        EvidenceSource(Path(manifest), Path(runtime_root))
        for manifest, runtime_root in zip(args.manifest, args.runtime_root)
    ]
    index = load_b2_evidence(
        sources=sources,
        requests_root=Path(args.requests_root),
        expected_count=args.expected_count,
    )
    plan = build_component_plan(index)
    caps = ConsolidationCaps(
        max_components=args.max_components,
        max_members=args.max_members,
        max_unique_blocks=args.max_unique_blocks,
        prompt_token_cap=args.prompt_token_cap,
    )
    packets, dry = packetize_components(plan=plan, index=index, caps=caps)
    write_plan_artifacts(
        out_dir=Path(args.out),
        index=index,
        plan=plan,
        packets=packets,
        dry_render=dry,
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "no_api_called": True,
                "index_sha256": index["index_sha256"],
                "plan_sha256": plan["plan_sha256"],
                "dry_render_sha256": dry["dry_render_sha256"],
                "counts": plan["counts"],
                "dry_totals": dry["totals"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
