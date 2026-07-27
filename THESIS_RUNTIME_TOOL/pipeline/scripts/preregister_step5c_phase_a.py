"""Build the public, label-free S5C Phase-A preregistration artifacts."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping

from pipeline.literary.builder_v3_pipeline import _build_windows
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.step5c_slice import (
    build_public_heldout_manifest,
    prompt_manifest,
)


CANARY_TERMS = (
    "madam",
    "the master",
    "the mistress",
    "young master",
    "old master",
    "jabez",
    "catherine",
)
SEED = "m4f-s5c-heldout-v1-20260713"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _blocks(document: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(block["block_id"]): unicodedata.normalize(
            "NFC", str(block.get("clean_text") or block.get("source_text") or "")
        )
        for chapter in (document.get("chapters") or [])[:4]
        for block in chapter.get("blocks") or []
    }


def _unique_span(text: str, surface: str) -> tuple[int, int] | None:
    matches = list(re.finditer(re.escape(surface), text, re.IGNORECASE))
    return matches[0].span() if len(matches) == 1 else None


def _source_universe(runtime_root: Path) -> list[dict[str, Any]]:
    document = _read(
        runtime_root
        / "data"
        / "reports"
        / "literary_l2a0_wh_builder_scaffold"
        / "document.json"
    )
    blocks = _blocks(document)
    base = runtime_root / "data" / "reports" / "literary_m4_full"
    by_id: dict[str, dict[str, Any]] = {}

    for path in sorted((base / "lexicon").glob("wb_wh_ch0[1-4]_*.json")):
        parsed = _read(path).get("parsed_json") or {}
        for mention in parsed.get("character_mentions") or []:
            occurrence_id = str(mention.get("mention_id") or "")
            surface = unicodedata.normalize("NFC", str(mention.get("surface") or ""))
            block_id = str((mention.get("block_ids") or [""])[0])
            span = _unique_span(blocks.get(block_id, ""), surface)
            if not occurrence_id or span is None:
                continue
            row = {
                "occurrence_id": occurrence_id,
                "chapter_id": str(parsed.get("chapter_id") or ""),
                "block_id": block_id,
                "occurrence_kind": "mention",
                "mention_type": str(mention.get("mention_type") or ""),
                "surface": surface,
                "source_anchor": {
                    "block_id": block_id,
                    "char_start": span[0],
                    "char_end": span[1],
                },
                "source_artifact": path.relative_to(runtime_root).as_posix(),
            }
            existing = by_id.get(occurrence_id)
            if existing is not None and existing != row:
                raise ValueError(f"conflicting source occurrence: {occurrence_id}")
            by_id[occurrence_id] = row

    for path in sorted((base / "narrative").glob("wb_wh_ch0[1-4]_*.json")):
        parsed = _read(path).get("parsed_json") or {}
        for turn in parsed.get("speaker_turns") or []:
            surface = unicodedata.normalize(
                "NFC", str(turn.get("address_term_used") or "")
            )
            block_id = str(turn.get("block_id") or "")
            span = _unique_span(blocks.get(block_id, ""), surface)
            if not surface or span is None:
                continue
            occurrence_id = f"endpoint_{turn.get('turn_id')}_addressee"
            row = {
                "occurrence_id": occurrence_id,
                "chapter_id": str(parsed.get("chapter_id") or ""),
                "block_id": block_id,
                "occurrence_kind": "endpoint",
                "mention_type": None,
                "surface": surface,
                "source_anchor": {
                    "block_id": block_id,
                    "char_start": span[0],
                    "char_end": span[1],
                },
                "source_artifact": path.relative_to(runtime_root).as_posix(),
            }
            existing = by_id.get(occurrence_id)
            if existing is not None and existing != row:
                raise ValueError(f"conflicting source occurrence: {occurrence_id}")
            by_id[occurrence_id] = row
    return sorted(
        by_id.values(),
        key=lambda row: (
            row["chapter_id"],
            row["block_id"],
            row["source_anchor"]["char_start"],
            row["occurrence_id"],
        ),
    )


def _canary_rows(universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        deepcopy(row)
        for row in universe
        if any(term in row["surface"].casefold() for term in CANARY_TERMS)
    ]
    return rows


def _call_graph(runtime_root: Path, *, canary_count: int, heldout_count: int) -> dict[str, Any]:
    document = _read(
        runtime_root
        / "data"
        / "reports"
        / "literary_l2a0_wh_builder_scaffold"
        / "document.json"
    )
    chapters = list(document.get("chapters") or [])[:4]
    window_count = sum(
        len(_build_windows(chapter, target_tokens=500, max_blocks=8, tail_k=2))
        for chapter in chapters
    )
    target_count = canary_count + heldout_count
    frame_ablation_reserve = canary_count
    rows = [
        {"stage": "B1-v3", "calls": window_count, "bucket_class": "gpt-5.4-mini", "max_output_tokens": 6144},
        {"stage": "B2-v3", "calls": window_count, "bucket_class": "gpt-5.4-mini", "max_output_tokens": 6144},
        {"stage": "B3-v3", "calls": 4, "bucket_class": "gpt-5.4", "max_output_tokens": 12288},
        {"stage": "frame-primary", "calls": 4, "bucket_class": "gpt-5.4", "max_output_tokens": 12288},
        {"stage": "frame-unqualified-measurement", "calls": 4, "bucket_class": "gemini-3.1-flash-lite", "max_output_tokens": 8192},
        {"stage": "identity-retrieval-worst-case", "calls": target_count, "bucket_class": "gpt-5.4", "max_output_tokens": 12288},
        {"stage": "identity-proposal-worst-case", "calls": target_count, "bucket_class": "gpt-5.4", "max_output_tokens": 12288},
        {"stage": "no-frame-contingent-worst-case", "calls": frame_ablation_reserve, "bucket_class": "gpt-5.4", "max_output_tokens": 12288},
    ]
    for row in rows:
        row["completion_token_upper"] = row["calls"] * row["max_output_tokens"]
    return {
        "schema_version": "literary_s5c_dry_call_graph_v2",
        "knowledge_scope": "pilot_ch1_4_frozen",
        "window_count": window_count,
        "target_count": target_count,
        "rows": rows,
        "total_calls_upper": sum(row["calls"] for row in rows),
        "completion_tokens_upper": sum(row["completion_token_upper"] for row in rows),
        "status": "phase_b_blocked_pending_incremental_render_and_user_approval",
        "note": "Worst-case one target per identity batch; batching may lower calls but may not lower evidence.",
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--codex-label-commitment", required=True)
    args = parser.parse_args()
    runtime_root = args.runtime_root.resolve()
    out_dir = args.out_dir.resolve()
    allowed = (runtime_root / "data" / "reports" / "literary_m4f_s5c_slice").resolve()
    if out_dir != allowed and allowed not in out_dir.parents:
        raise ValueError("preregistration output escapes slice report root")

    universe = _source_universe(runtime_root)
    canaries = _canary_rows(universe)
    canary_ids = [row["occurrence_id"] for row in canaries]
    heldout = build_public_heldout_manifest(
        universe,
        seed=SEED,
        canary_occurrence_ids=canary_ids,
    )
    heldout["label_commitments"]["codex"] = args.codex_label_commitment
    heldout["manifest_hash"] = canonical_hash(
        {key: value for key, value in heldout.items() if key != "manifest_hash"}
    )
    canary_manifest = {
        "schema_version": "literary_s5c_canary_manifest_v1",
        "selection_rule": {"surface_contains_casefold": list(CANARY_TERMS)},
        "source_universe_hash": canonical_hash(universe),
        "targets": canaries,
    }
    canary_manifest["manifest_hash"] = canonical_hash(canary_manifest)

    decision_table = {
        "schema_version": "literary_s5c_decision_table_v1",
        "rules": [
            {"condition": "selected source occurrence absent from v3", "outcome": "upstream_occurrence_missing", "root_cause": "context_missing"},
            {"condition": "retrieval status unknown", "outcome": "unknown", "root_cause": "indeterminate"},
            {"condition": "needed candidate absent after full-roster delivery", "outcome": "wrong", "root_cause": "retrieval_miss"},
            {"condition": "wrong with any delivery/prompt/validator gate unproven", "outcome": "wrong", "root_cause": "indeterminate"},
            {"condition": "wrong with all gates proven and known noise absent", "outcome": "wrong", "root_cause": "model_error"},
            {"condition": "wrong with preregistered bad-frame and no-frame flip", "outcome": "wrong", "root_cause": "upstream_frame_error"},
        ],
    }
    decision_table["manifest_hash"] = canonical_hash(decision_table)

    ablation = {
        "schema_version": "literary_s5c_ablation_manifest_v2",
        "paired_policy_sections": [
            "legacy_hint_noise",
            "full_block_context",
            "b0_removed_active_block_only",
        ],
        "superseded_policy_section": "b0_untrusted_cast_claims",
        "fixed_fields": ["prompt_sha256", "model_config_hash", "target_ids", "output_schema_hash"],
        "no_frame_contingent_target_ids": sorted(
            row["occurrence_id"] for row in canaries if row["chapter_id"] == "wh_ch03"
        ),
        "historical_comparison_kind": "system_intervention",
    }
    ablation["manifest_hash"] = canonical_hash(ablation)

    quota = {
        "schema_version": "literary_s5c_quota_manifest_v1",
        "utc_day_required": True,
        "accounting_formula": "prompt_tokens+completion_tokens",
        "buckets": [
            *[
                {"quota_bucket_id": f"openai-key-{key}-gpt54", "model_class": "gpt-5.4", "hard_daily_tokens": 250000, "internal_daily_tokens": 225000}
                for key in (1, 2)
            ],
            *[
                {"quota_bucket_id": f"openai-key-{key}-mini", "model_class": "gpt-5.4-mini", "hard_daily_tokens": 2500000, "internal_daily_tokens": 2250000}
                for key in (1, 2)
            ],
            *[
                {"quota_bucket_id": f"gemini-free-row-{key}", "model_class": "gemini-3.1-flash-lite", "rpm": 15, "tpm": 250000, "rpd": 500, "internal_experiment_tokens": 225000}
                for key in range(1, 6)
            ],
        ],
        "rotation_policy": "explicit_unit_boundary_only",
    }
    quota["manifest_hash"] = canonical_hash(quota)

    forbidden = {
        "schema_version": "literary_s5c_forbidden_boundary_v1",
        "allowed_write_root": "data/reports/literary_m4f_s5c_slice/<run_id>/",
        "forbidden_calls": ["promote", "overlay_apply", "quarantine_closure", "dependency_invalidation", "production_CAS_publish"],
        "forbidden_roots": ["data/reports/literary_m4d_b4v2", "data/jobs/d2l_p1/memory.sqlite3"],
        "phase_b_authorization": "blocked_pending_claude_accept_and_user_budget_approval",
    }
    forbidden["manifest_hash"] = canonical_hash(forbidden)

    call_graph = _call_graph(
        runtime_root,
        canary_count=len(canaries),
        heldout_count=len(heldout["targets"]),
    )
    call_graph["manifest_hash"] = canonical_hash(call_graph)
    prompts = prompt_manifest(runtime_root.parent / "design" / "LITERARY_PROMPT_DESIGN.md")
    config_files = [
        runtime_root / "pipeline" / "configs" / "llm_s5c_slice_m1.yaml",
        runtime_root / "pipeline" / "configs" / "llm_s5c_slice_gpt54.yaml",
        runtime_root / "pipeline" / "configs" / "gemini_s5c_frame_measurement.yaml",
    ]
    config_manifest = {
        "schema_version": "literary_s5c_config_manifest_v1",
        "configs": [
            {
                "path": path.relative_to(runtime_root).as_posix(),
                "sha256": _file_sha256(path),
            }
            for path in config_files
        ],
    }
    config_manifest["manifest_hash"] = canonical_hash(config_manifest)

    _write_json(out_dir / "canary_manifest.json", canary_manifest)
    _write_json(out_dir / "heldout_public_manifest.json", heldout)
    _write_json(out_dir / "decision_table.json", decision_table)
    _write_json(out_dir / "context_ablation_manifest.json", ablation)
    _write_json(out_dir / "quota_bucket_manifest.json", quota)
    _write_json(out_dir / "forbidden_boundary_manifest.json", forbidden)
    _write_json(out_dir / "dry_call_graph.json", call_graph)
    _write_json(out_dir / "prompt_manifest.json", prompts)
    _write_json(out_dir / "config_manifest.json", config_manifest)
    phase_a_manifest = {
        "schema_version": "literary_s5c_phase_a_manifest_v1",
        "knowledge_scope": "pilot_ch1_4_frozen",
        "source_occurrence_count": len(universe),
        "canary_target_count": len(canaries),
        "heldout_target_count": len(heldout["targets"]),
        "artifact_hashes": {
            "canary_manifest": canary_manifest["manifest_hash"],
            "heldout_public_manifest": heldout["manifest_hash"],
            "decision_table": decision_table["manifest_hash"],
            "context_ablation_manifest": ablation["manifest_hash"],
            "quota_bucket_manifest": quota["manifest_hash"],
            "forbidden_boundary_manifest": forbidden["manifest_hash"],
            "dry_call_graph": call_graph["manifest_hash"],
            "prompt_manifest": prompts["manifest_hash"],
            "config_manifest": config_manifest["manifest_hash"],
        },
        "codex_label_commitment": args.codex_label_commitment,
        "claude_label_commitment": None,
        "phase_b_authorization": forbidden["phase_b_authorization"],
        "zero_api": True,
    }
    phase_a_manifest["manifest_hash"] = canonical_hash(phase_a_manifest)
    _write_json(out_dir / "phase_a_manifest.json", phase_a_manifest)
    print(
        canonical_json(
            {
                "source_occurrences": len(universe),
                "canary_targets": len(canaries),
                "heldout_targets": len(heldout["targets"]),
                "heldout_manifest_hash": heldout["manifest_hash"],
                "total_calls_upper": call_graph["total_calls_upper"],
                "phase_b_authorization": forbidden["phase_b_authorization"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
