from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pipeline.prepass.d2l_b2_packet_plan_v2 import (
    B2PacketPlanError,
    PacketCaps,
    build_candidate_index,
    build_packet_plan,
    canonical_sha256,
    load_sealed_json,
    plan_from_paths,
    select_ordered_spread,
    write_plan,
)


def _source() -> dict:
    windows = []
    for window_index in range(2):
        blocks = [
            [
                f"b{block_index}",
                f"Source block {block_index} contains reusable technical text.",
            ]
            for block_index in range(window_index * 4 + 1, window_index * 4 + 5)
        ]
        windows.append(
            {
                "window_id": f"w{window_index + 1}",
                "window_order": window_index,
                "source_blocks": blocks,
            }
        )
    payload = {
        "manifest_version": "fixture",
        "chapter_id": "chapter",
        "windows": windows,
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    return payload


def _aggregate() -> dict:
    rows = [
        {
            "source_surface": "Technical Unit",
            "source_block_ids": ["b1", "b5"],
            "window_ids": ["w1", "w2"],
        },
        {
            "source_surface": "technical unit",
            "source_block_ids": ["b2"],
            "window_ids": ["w1"],
        },
        {
            "source_surface": "technical units",
            "source_block_ids": ["b3"],
            "window_ids": ["w1"],
        },
        {
            "source_surface": "larger technical unit",
            "source_block_ids": ["b4"],
            "window_ids": ["w1"],
        },
        {
            "source_surface": "widely reused unit",
            "source_block_ids": [f"b{index}" for index in range(1, 9)],
            "window_ids": ["w1", "w2"],
        },
    ]
    payload = {"aggregate_version": "fixture", "candidates": rows}
    payload["aggregate_sha256"] = canonical_sha256(payload)
    return payload


def test_index_groups_normalized_exact_only_and_exact_covers_rows() -> None:
    index = build_candidate_index(_aggregate(), _source())
    assert index["summary"]["aggregate_rows"] == 5
    assert index["summary"]["candidate_rows"] == 4
    assert index["summary"]["source_rows_exact_covered"] == 5
    by_normalized = {row["normalized_surface"]: row for row in index["candidates"]}
    assert by_normalized["technical unit"]["surfaces"] == [
        "Technical Unit",
        "technical unit",
    ]
    assert "technical units" in by_normalized
    assert "larger technical unit" in by_normalized
    assert len(by_normalized["technical unit"]["source_row_hashes"]) == 2


def test_index_ids_and_serialization_are_input_order_independent() -> None:
    aggregate = _aggregate()
    forward = build_candidate_index(aggregate, _source())
    aggregate["candidates"].reverse()
    reverse = build_candidate_index(aggregate, _source())
    assert forward == reverse


def test_index_rejects_unknown_window_provenance() -> None:
    aggregate = _aggregate()
    aggregate["candidates"][0]["window_ids"] = ["missing-window"]
    with pytest.raises(B2PacketPlanError, match="windows are invalid"):
        build_candidate_index(aggregate, _source())


def test_ordered_spread_preserves_first_last_and_cap() -> None:
    assert select_ordered_spread(["a", "b", "c"], 4) == ["a", "b", "c"]
    assert select_ordered_spread(list("abcdefgh"), 4) == ["a", "c", "f", "h"]
    assert select_ordered_spread(list("abcdefgh"), 1) == ["a"]


def test_partial_evidence_keeps_all_support_provenance() -> None:
    index = build_candidate_index(_aggregate(), _source(), max_evidence_blocks=4)
    row = next(
        value
        for value in index["candidates"]
        if value["normalized_surface"] == "widely reused unit"
    )
    assert row["source_block_ids"] == [f"b{index}" for index in range(1, 9)]
    assert row["evidence_block_ids"] == ["b1", "b3", "b6", "b8"]
    assert row["evidence_complete"] is False


def test_packet_plan_exact_covers_candidates_and_deduplicates_blocks() -> None:
    source = _source()
    index = build_candidate_index(_aggregate(), source)
    plan, packets = build_packet_plan(
        index,
        source,
        caps=PacketCaps(
            max_candidates=3,
            max_unique_blocks=8,
            prompt_token_cap=6000,
            max_evidence_blocks_per_candidate=4,
        ),
    )
    owners = [
        candidate_id
        for row in plan["packets"]
        for candidate_id in row["candidate_ids"]
    ]
    assert len(owners) == len(set(owners)) == 4
    assert plan["summary"]["candidate_exact_cover"] == 4
    assert all(row["candidate_count"] <= 3 for row in plan["packets"])
    for payload in packets.values():
        block_ids = [row["block_id"] for row in payload["packet"]["source_blocks"]]
        assert len(block_ids) == len(set(block_ids))
        assert all(
            "source_row_hashes" not in row
            for row in payload["packet"]["candidates"]
        )


def test_same_window_candidates_are_preferred_when_caps_allow() -> None:
    source = _source()
    index = build_candidate_index(_aggregate(), source)
    plan, _ = build_packet_plan(
        index,
        source,
        caps=PacketCaps(
            max_candidates=3,
            max_unique_blocks=8,
            prompt_token_cap=6000,
            max_evidence_blocks_per_candidate=4,
        ),
    )
    first_ids = set(plan["packets"][0]["candidate_ids"])
    by_surface = {row["normalized_surface"]: row["candidate_id"] for row in index["candidates"]}
    assert by_surface["technical unit"] in first_ids
    assert by_surface["technical units"] in first_ids


def test_too_small_prompt_cap_fails_closed() -> None:
    source = _source()
    index = build_candidate_index(_aggregate(), source)
    with pytest.raises(B2PacketPlanError, match="cannot fit"):
        build_packet_plan(
            index,
            source,
            caps=PacketCaps(
                max_candidates=1,
                max_unique_blocks=4,
                prompt_token_cap=1,
                max_evidence_blocks_per_candidate=4,
            ),
        )


def test_load_sealed_json_rejects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "aggregate.json"
    payload = _aggregate()
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_sealed_json(
        path,
        hash_field="aggregate_sha256",
        expected_hash=payload["aggregate_sha256"],
    )
    assert loaded == payload
    payload["candidates"][0]["source_surface"] = "changed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(B2PacketPlanError, match="content hash mismatch"):
        load_sealed_json(path, hash_field="aggregate_sha256")


def test_plan_from_paths_writes_immutable_offline_artifacts(tmp_path: Path) -> None:
    aggregate_path = tmp_path / "aggregate.json"
    source_path = tmp_path / "source.json"
    aggregate_path.write_text(json.dumps(_aggregate()), encoding="utf-8")
    source_path.write_text(json.dumps(_source()), encoding="utf-8")
    out = tmp_path / "out"
    dry = plan_from_paths(
        aggregate_path=aggregate_path,
        source_manifest_path=source_path,
        out_dir=out,
        expected_aggregate_sha256=None,
        expected_source_manifest_sha256=None,
    )
    assert dry["no_api_called"] is True
    assert dry["gold_loaded"] is False
    assert dry["historical_notebook_loaded"] is False
    assert dry["summary"]["aggregate_rows"] == 5
    assert dry["summary"]["candidate_rows"] == 4
    assert dry["summary"]["grouped_rows"] == 1
    assert dry["summary"]["caps_satisfied"] is True
    assert dry["summary"]["source_block_renders_across_packets"] > 0
    assert dry["source_manifest_sha256"] == _source()["manifest_sha256"]
    assert dry["aggregate_sha256"] == _aggregate()["aggregate_sha256"]
    assert (out / "candidate_index.json").exists()
    assert (out / "packet_plan.json").exists()
    request_paths = list((out / "packets").glob("*/request.json"))
    assert len(request_paths) == dry["summary"]["packets"]
    assert plan_from_paths(
        aggregate_path=aggregate_path,
        source_manifest_path=source_path,
        out_dir=out,
        expected_aggregate_sha256=None,
        expected_source_manifest_sha256=None,
    ) == dry


def test_write_plan_refuses_changed_artifact(tmp_path: Path) -> None:
    source = _source()
    index = build_candidate_index(_aggregate(), source)
    plan, packets = build_packet_plan(index, source)
    write_plan(out_dir=tmp_path, candidate_index=index, packet_plan=plan, packets=packets)
    (tmp_path / "candidate_index.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(B2PacketPlanError, match="Refusing to overwrite"):
        write_plan(
            out_dir=tmp_path,
            candidate_index=index,
            packet_plan=plan,
            packets=packets,
        )


def test_planner_has_no_api_or_gold_dependency() -> None:
    module_path = Path(__file__).parents[1] / "prepass" / "d2l_b2_packet_plan_v2.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert not any(
        value.startswith(("requests", "httpx", "socket", "openai", "google"))
        for value in imports
    )
    assert "eval_glossary_gold" not in source
    assert "baseline_notebook" not in source
