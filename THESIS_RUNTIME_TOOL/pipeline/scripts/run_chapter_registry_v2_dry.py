from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from statistics import median
from typing import Any, Mapping, Sequence

from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.chapter_registry_schema_v2 import (
    ALIAS_SCOPE_POLICY_VERSION,
    AUDIT_SCHEMA_VERSION,
    B2_RESCAN_POLICY_VERSION,
    CANDIDATE_POLICY_VERSION,
    CLEAN_POLICY_VERSION,
    DELTA_SCHEMA_VERSION,
    ORIENTATION_SCHEMA_VERSION,
    PROMPT_IDS,
    REGISTRY_SCHEMA_VERSION,
    RunConfigV2,
)
from pipeline.literary.chapter_registry_v2 import (
    ChapterWorkingRegistryV2,
    build_registry_windows,
    chapter_source_manifest_hash,
    empty_registry_snapshot_v2,
    estimate_registry_prompt_tokens,
    render_b0_request,
    render_b1_request,
)


REPORT_SCHEMA_VERSION = "literary_m4f_b1_prejoined_context_dry_render_v1"
RUN_CONFIG_SCHEMA_VERSION = "literary_m4f_b0b1_run_config_v2"
CHAPTER_IDS = ("wh_ch01", "wh_ch02")

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DOCUMENT = (
    RUNTIME_ROOT / "data" / "reports" / "literary_l2a0_wh_builder_scaffold" / "document.json"
)
DEFAULT_DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
DEFAULT_OUTPUT = (
    RUNTIME_ROOT / "data" / "reports" / "literary_m4f_b1_prejoined_context_dry_20260714"
)


class DryRenderError(RuntimeError):
    """Raised when Phase B cannot produce a trustworthy bounded dry-render."""


def proposed_run_config() -> RunConfigV2:
    unknown = {"rpm": None, "tpm": None, "rpd": None}
    quota_gates: dict[str, dict[str, Any]] = {}
    for key_label in ("openai-row1", "openai-row2"):
        quota_gates[f"{key_label}-gpt54"] = {
            "quota_bucket_id": key_label,
            "model_id": "gpt-5.4",
            **unknown,
            "internal_utc_day_token_cap": 225000,
        }
        quota_gates[f"{key_label}-mini"] = {
            "quota_bucket_id": key_label,
            "model_id": "gpt-5.4-mini",
            **unknown,
            "internal_utc_day_token_cap": 2250000,
        }
    return RunConfigV2(
        b0_model_id="gpt-5.4",
        b0_reasoning_effort="none",
        b0_temperature=1.0,
        b0_seed=20260612,
        b0_verbosity="low",
        b0_output_cap=2048,
        b1_model_id="gpt-5.4-mini",
        b1_reasoning_effort="none",
        b1_temperature=1.0,
        b1_seed=20260612,
        b1_verbosity="low",
        b1_output_cap=4096,
        auditor_model_id="gpt-5.4",
        auditor_reasoning_effort="none",
        auditor_temperature=1.0,
        auditor_seed=20260612,
        auditor_verbosity="low",
        auditor_output_cap=8192,
        b1_window_target_tokens=500,
        b1_window_max_blocks=8,
        context_only_tail_k=2,
        recency_k=8,
        candidate_card_count_cap=16,
        candidate_card_token_cap=3500,
        targeted_recall_call_cap=4,
        auditor_component_cap=8,
        auditor_input_token_cap=12000,
        auditor_exception_share_cap=0.35,
        b0_input_cap=14000,
        b1_input_cap=12000,
        pricing_usd_per_million={
            "b0": {"input": None, "cached_input": None, "output": None},
            "b1": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
            "auditor": {"input": None, "cached_input": None, "output": None},
        },
        quota_gates=quota_gates,
        role_quota_gate_ids={
            "b0": ("openai-row1-gpt54", "openai-row2-gpt54"),
            "b1": ("openai-row1-mini", "openai-row2-mini"),
            "auditor": ("openai-row1-gpt54", "openai-row2-gpt54"),
        },
        prompt_versions=PROMPT_IDS,
        schema_versions={
            "registry": REGISTRY_SCHEMA_VERSION,
            "orientation": ORIENTATION_SCHEMA_VERSION,
            "delta": DELTA_SCHEMA_VERSION,
            "audit": AUDIT_SCHEMA_VERSION,
        },
        validator_version="chapter_registry_validator_v2_2",
        policy_versions={
            "candidate_selection": CANDIDATE_POLICY_VERSION,
            "clean_commit": CLEAN_POLICY_VERSION,
            "b2_rescan": B2_RESCAN_POLICY_VERSION,
            "alias_scope": ALIAS_SCOPE_POLICY_VERSION,
        },
    )


def _load_chapters(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DryRenderError("document must be an object")
    by_id = {
        str(row.get("chapter_id") or ""): row
        for row in payload.get("chapters") or []
        if isinstance(row, dict)
    }
    missing = [chapter_id for chapter_id in CHAPTER_IDS if chapter_id not in by_id]
    if missing:
        raise DryRenderError(f"source document lacks chapters: {missing}")
    return [dict(by_id[chapter_id]) for chapter_id in CHAPTER_IDS]


def _candidate_surfaces(
    blocks: Sequence[Mapping[str, Any]], count: int
) -> list[tuple[str, str]]:
    occurrences: list[tuple[str, str, int]] = []
    folded_counts: Counter[str] = Counter()
    seen_folded: set[str] = set()
    ordinal = 0
    for block in blocks:
        block_id = str(block.get("block_id") or "")
        text = str(block.get("clean_text") or block.get("source_text") or "")
        for match in re.finditer(r"[A-Za-z][A-Za-z'-]{2,}", text):
            surface = match.group(0)
            folded = surface.casefold()
            folded_counts[folded] += 1
            if folded not in seen_folded:
                seen_folded.add(folded)
                occurrences.append((surface, block_id, ordinal))
                ordinal += 1
    ranked = sorted(
        occurrences,
        key=lambda row: (
            folded_counts[row[0].casefold()] != 1,
            folded_counts[row[0].casefold()],
            row[2],
        ),
    )
    selected = [(surface, block_id) for surface, block_id, _ in ranked[:count]]
    if len(selected) != count:
        raise DryRenderError(
            f"cannot derive {count} distinct dry-only candidate surfaces from active window"
        )
    return selected


def _saturated_snapshot(
    *,
    state_lineage_id: str,
    surfaces: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    snapshot = empty_registry_snapshot_v2(state_lineage_id)
    entities: list[dict[str, Any]] = []
    for index, (surface, support_block_id) in enumerate(surfaces):
        body = {
            "entity_id": f"ent2_dry_{index:02d}",
            "canonical_surface": surface,
            "referent_kind": "person",
            "identity_summary": (
                f"Dry-render candidate {index:02d}; synthetic card used only to measure the "
                "locked candidate envelope."
            ),
            "created_from_block_ids": [support_block_id],
            "support_block_ids": [support_block_id],
            "status": "confirmed",
        }
        entities.append({**body, "revision_hash": canonical_hash(body)})
    snapshot["entities"] = entities
    snapshot["snapshot_hash"] = canonical_hash(
        {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    )
    return snapshot


def _percentile(values: Sequence[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return int(ordered[index])


def _stats(values: Sequence[int]) -> dict[str, int | float]:
    return {
        "count": len(values),
        "min": min(values) if values else 0,
        "p50": float(median(values)) if values else 0,
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else 0,
        "sum": sum(values),
    }


def _configured_cost(
    *,
    prompt_tokens: int,
    output_tokens: int,
    input_rate: float | None,
    output_rate: float | None,
) -> float | None:
    if input_rate is None or output_rate is None:
        return None
    return round((prompt_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 6)


def dry_render(
    *,
    document_path: Path,
    design_doc: Path,
    output_dir: Path,
    run_config: RunConfigV2 | None = None,
) -> dict[str, Any]:
    config = run_config or proposed_run_config()
    chapters = _load_chapters(document_path)
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise DryRenderError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    rendered_rows: list[dict[str, Any]] = []
    chapter_rows: list[dict[str, Any]] = []
    ordinary_prompt_tokens: list[int] = []
    saturated_prompt_tokens: list[int] = []
    selected_counts: list[int] = []
    selected_token_counts: list[int] = []
    candidate_packet_counts: list[int] = []
    packet_candidate_counts: list[int] = []
    unmatched_recency_counts: list[int] = []
    legacy_context_bytes: list[int] = []
    prejoined_context_bytes: list[int] = []
    saturated_overflows = 0
    b0_prompt_tokens_total = 0
    ordinary_window_calls = 0

    for chapter in chapters:
        chapter_id = str(chapter["chapter_id"])
        b0_request = render_b0_request(
            chapter=chapter, design_doc=design_doc, run_config=config
        )
        b0_tokens = estimate_registry_prompt_tokens(b0_request.messages)
        b0_prompt_tokens_total += b0_tokens
        rendered_rows.append(
            {
                "scenario": "exact_source",
                "prompt_tokens": b0_tokens,
                "cache_eligible": True,
                "cache_key_fields": [
                    "role",
                    "chapter_id",
                    "prompt_sha256",
                    "model_contract",
                    "source_manifest",
                    "output_schema_version",
                    "run_config_hash",
                ],
                "request": b0_request.to_dict(),
            }
        )
        windows = build_registry_windows(
            chapter,
            target_tokens=config.b1_window_target_tokens,
            max_blocks=config.b1_window_max_blocks,
            preceding_tail_k=config.context_only_tail_k,
        )
        ordinary_window_calls += len(windows)
        block_order = {
            str(row["block_id"]): int(row.get("order_index") or 0)
            for row in chapter.get("blocks") or []
        }
        empty_working = ChapterWorkingRegistryV2.create(
            state_lineage_id="dry-wh",
            chapter_id=chapter_id,
            source_manifest_hash=chapter_source_manifest_hash(chapter),
            parent_snapshot=empty_registry_snapshot_v2("dry-wh"),
        )
        chapter_ordinary: list[int] = []
        chapter_saturated: list[int] = []
        for window in windows:
            ordinary = render_b1_request(
                chapter_id=chapter_id,
                window_id=str(window["window_id"]),
                b0_gist="[DRY_RENDER_B0_GIST_PLACEHOLDER]",
                active_blocks=window["blocks"],
                context_only_tail=window["context_only_tail"],
                working=empty_working,
                block_order=block_order,
                design_doc=design_doc,
                run_config=config,
            )
            ordinary_tokens = estimate_registry_prompt_tokens(ordinary.messages)
            ordinary_prompt_tokens.append(ordinary_tokens)
            chapter_ordinary.append(ordinary_tokens)
            rendered_rows.append(
                {
                    "scenario": "exact_source_empty_registry",
                    "prompt_tokens": ordinary_tokens,
                    "cache_eligible": True,
                    "cache_key_fields": [
                        "role",
                        "chapter_id",
                        "window_id",
                        "prompt_sha256",
                        "model_contract",
                        "source_block_manifest",
                        "context_only_tail_manifest",
                        "b0_gist_hash",
                        "parent_working_revision_hash",
                        "candidate_manifest_hash",
                        "output_schema_version",
                        "run_config_hash",
                    ],
                    "request": ordinary.to_dict(),
                }
            )

            dry_surfaces = _candidate_surfaces(
                window["blocks"], config.candidate_card_count_cap
            )
            saturated_working = ChapterWorkingRegistryV2.create(
                state_lineage_id="dry-wh",
                chapter_id=chapter_id,
                source_manifest_hash=chapter_source_manifest_hash(chapter),
                parent_snapshot=_saturated_snapshot(
                    state_lineage_id="dry-wh",
                    surfaces=dry_surfaces,
                ),
            )
            saturated = render_b1_request(
                chapter_id=chapter_id,
                window_id=f"{window['window_id']}:cap-saturated",
                b0_gist="[DRY_RENDER_B0_GIST_PLACEHOLDER]",
                active_blocks=window["blocks"],
                context_only_tail=window["context_only_tail"],
                working=saturated_working,
                block_order=block_order,
                design_doc=design_doc,
                run_config=config,
            )
            saturated_tokens = estimate_registry_prompt_tokens(saturated.messages)
            saturated_prompt_tokens.append(saturated_tokens)
            chapter_saturated.append(saturated_tokens)
            manifest = saturated.sections["candidate_selection_manifest"]
            selected_counts.append(int(manifest["selected_count"]))
            selected_token_counts.append(int(manifest["selected_token_estimate"]))
            candidate_packet_counts.append(int(manifest["surface_candidate_packet_count"]))
            packet_candidate_counts.append(int(manifest["packet_candidate_count"]))
            unmatched_recency_counts.append(len(saturated.sections["unmatched_recency_cards"]))
            legacy_context_bytes.append(int(manifest["legacy_separate_context_bytes"]))
            prejoined_context_bytes.append(int(manifest["prejoined_context_bytes"]))
            saturated_overflows += int(bool(manifest["overflow"]))
            rendered_rows.append(
                {
                    "scenario": "exact_source_cap_saturated_registry",
                    "prompt_tokens": saturated_tokens,
                    "cache_eligible": True,
                    "candidate_manifest": manifest,
                    "request": saturated.to_dict(),
                }
            )
        chapter_rows.append(
            {
                "chapter_id": chapter_id,
                "block_count": len(chapter.get("blocks") or []),
                "source_manifest_hash": chapter_source_manifest_hash(chapter),
                "b0_prompt_tokens": b0_tokens,
                "ordinary_b1_calls": len(windows),
                "ordinary_b1_prompt_tokens": _stats(chapter_ordinary),
                "cap_saturated_b1_prompt_tokens": _stats(chapter_saturated),
            }
        )

    chapter_count = len(chapters)
    targeted_call_reserve = config.targeted_recall_call_cap * chapter_count
    auditor_call_reserve = config.auditor_component_cap * chapter_count
    saturated_max_by_chapter = {
        row["chapter_id"]: int(row["cap_saturated_b1_prompt_tokens"]["max"])
        for row in chapter_rows
    }
    targeted_prompt_reserve = sum(
        saturated_max_by_chapter[row["chapter_id"]] * config.targeted_recall_call_cap
        for row in chapter_rows
    )
    auditor_prompt_reserve = auditor_call_reserve * config.auditor_input_token_cap
    b1_prompt_upper = sum(saturated_prompt_tokens) + targeted_prompt_reserve
    b1_output_upper = (ordinary_window_calls + targeted_call_reserve) * config.b1_output_cap
    gpt_prompt_upper = b0_prompt_tokens_total + auditor_prompt_reserve
    gpt_output_upper = chapter_count * config.b0_output_cap + auditor_call_reserve * config.auditor_output_cap

    b0_pricing = config.pricing_usd_per_million["b0"]
    b1_pricing = config.pricing_usd_per_million["b1"]
    auditor_pricing = config.pricing_usd_per_million["auditor"]
    report_body = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "dry_render_only",
        "api_calls": 0,
        "chapters": chapter_rows,
        "source": {
            "path": document_path.resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            "sha256": file_sha256(document_path),
            "chapter_ids": list(CHAPTER_IDS),
        },
        "run_config_hash": config.config_hash,
        "call_counts": {
            "exact_source_minimum": {
                "b0": chapter_count,
                "ordinary_b1": ordinary_window_calls,
                "targeted_recall": 0,
                "auditor": 0,
                "total": chapter_count + ordinary_window_calls,
            },
            "reserved_upper_bound": {
                "b0": chapter_count,
                "ordinary_b1": ordinary_window_calls,
                "targeted_recall": targeted_call_reserve,
                "auditor": auditor_call_reserve,
                "total": chapter_count
                + ordinary_window_calls
                + targeted_call_reserve
                + auditor_call_reserve,
            },
        },
        "token_envelope": {
            "exact_source_empty_registry_prompt_tokens": b0_prompt_tokens_total
            + sum(ordinary_prompt_tokens),
            "ordinary_cap_saturated_prompt_tokens": b0_prompt_tokens_total
            + sum(saturated_prompt_tokens),
            "b1_prompt_upper_with_targeted_reserve": b1_prompt_upper,
            "b1_completion_cap_reserve": b1_output_upper,
            "gpt54_prompt_upper_with_auditor_reserve": gpt_prompt_upper,
            "gpt54_completion_cap_reserve": gpt_output_upper,
            "all_models_upper_prompt_plus_completion": b1_prompt_upper
            + b1_output_upper
            + gpt_prompt_upper
            + gpt_output_upper,
        },
        "candidate_envelope": {
            "selected_cards": _stats(selected_counts),
            "selected_card_tokens": _stats(selected_token_counts),
            "surface_candidate_packets": _stats(candidate_packet_counts),
            "packet_candidates": _stats(packet_candidate_counts),
            "unmatched_recency_cards": _stats(unmatched_recency_counts),
            "legacy_separate_context_bytes": _stats(legacy_context_bytes),
            "prejoined_context_bytes": _stats(prejoined_context_bytes),
            "prejoined_minus_legacy_context_bytes": sum(prejoined_context_bytes)
            - sum(legacy_context_bytes),
            "prejoined_to_legacy_context_ratio": (
                round(sum(prejoined_context_bytes) / sum(legacy_context_bytes), 6)
                if sum(legacy_context_bytes)
                else None
            ),
            "cap_saturated_overflow_windows": saturated_overflows,
            "note": (
                "Synthetic cards measure bounded context overhead and the exact prejoined-vs-legacy "
                "candidate-section bytes; they do not measure semantic quality."
            ),
        },
        "measurement_scope": {
            "b0": "exact prompt bytes over the real full chapter source",
            "ordinary_b1": (
                "exact real source windows with an empty registry and a fixed dry-only B0 gist; "
                "this is a lower-bound context state, not a simulated semantic run"
            ),
            "cap_saturated_b1": (
                "exact real source windows with 16 distinct source-matched synthetic cards; "
                "this measures the locked candidate envelope only"
            ),
            "auditor": "call/input/output reserves only; no exception manifest exists before Phase C",
        },
        "phase_b_hardening": [
            {
                "finding": "candidate-card caps did not bound repeated candidate-link payloads",
                "resolution": "added and enforced a total B1 input-token cap",
            },
            {
                "finding": "case-insensitive substring matching could map Arden inside garden",
                "resolution": "literal matching now enforces Unicode word boundaries",
            },
            {
                "finding": "the first synthetic saturation used one repeated surface for all cards",
                "resolution": (
                    "saturation uses distinct source-matched surfaces and reports packet/candidate counts"
                ),
            },
            {
                "finding": "B1 had to join candidate links to cards across separate arrays",
                "resolution": (
                    "code now emits one non-authoritative packet per source surface, aggregates block ids, "
                    "and includes each candidate card once per packet"
                ),
            },
        ],
        "configured_cost_upper_bound_usd": {
            "b1": _configured_cost(
                prompt_tokens=b1_prompt_upper,
                output_tokens=b1_output_upper,
                input_rate=b1_pricing["input"],
                output_rate=b1_pricing["output"],
            ),
            "b0": _configured_cost(
                prompt_tokens=b0_prompt_tokens_total,
                output_tokens=chapter_count * config.b0_output_cap,
                input_rate=b0_pricing["input"],
                output_rate=b0_pricing["output"],
            ),
            "auditor": _configured_cost(
                prompt_tokens=auditor_prompt_reserve,
                output_tokens=auditor_call_reserve * config.auditor_output_cap,
                input_rate=auditor_pricing["input"],
                output_rate=auditor_pricing["output"],
            ),
            "pricing_usd_per_million": asdict(config)["pricing_usd_per_million"],
            "note": "null means the monetary rate is not locked; token and quota gates still apply",
        },
        "cache": {
            "eligible": True,
            "assumed_hits_in_upper_bound": 0,
            "repeated_system_prompt_prefixes": {
                "b0": PROMPT_IDS["b0"],
                "b1": PROMPT_IDS["b1"],
                "auditor": PROMPT_IDS["auditor"],
            },
            "warning": "Provider cache support/hits remain unverified; quota accounting uses uncached tokens.",
        },
        "quota": {
            "quota_gates": asdict(config)["quota_gates"],
            "role_quota_gate_ids": asdict(config)["role_quota_gate_ids"],
            "unknown_provider_limit_gate_ids": list(config.unknown_provider_limit_gate_ids),
            "phase_c_blocked_on_unknown_provider_limits": bool(
                config.unknown_provider_limit_gate_ids
            ),
        },
        "approval": {
            "task_default": "explicit approval of the exact RunConfigV2 hash",
            "status": "waived_by_user_for_the_two_chapter_phase_c_pilot",
        },
        "stop_conditions": [
            "unknown provider RPM/TPM/RPD before API execution",
            "B0 input above configured full-chapter cap",
            "B1 input above configured total-request cap",
            "candidate or targeted-recall overflow beyond approved cap",
            "Auditor exception share or component/input cap exceeded",
            "internal UTC-day gate risk for any quota bucket",
            "request/schema/fingerprint drift",
            "frozen D2L DB mutation",
        ],
        "phase_c_ready": False,
        "phase_c_blockers": [
            "provider RPM/TPM/RPD values for both physical OpenAI key buckets",
            "real execution adapter/persistence orchestration is not implemented or gated",
        ],
    }
    report = {
        **report_body,
        "report_hash": canonical_hash(report_body),
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    config_payload = {
        "schema_version": RUN_CONFIG_SCHEMA_VERSION,
        "config_hash": config.config_hash,
        "config": config.to_dict(),
    }
    config_path = output / f"run_config_{config.config_hash[:16]}.json"
    config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "dry_render_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output / "rendered_requests.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in rendered_rows:
            handle.write(canonical_json(row) + "\n")
    summary = (
        "# M4f B1 Prejoined Candidate Context - Offline Dry Render\n\n"
        f"- RunConfig hash: `{config.config_hash}`\n"
        f"- Exact minimum calls: `{report['call_counts']['exact_source_minimum']['total']}`\n"
        f"- Reserved upper-bound calls: `{report['call_counts']['reserved_upper_bound']['total']}`\n"
        f"- Exact empty-registry prompt tokens: "
        f"`{report['token_envelope']['exact_source_empty_registry_prompt_tokens']}`\n"
        f"- Cap-saturated ordinary prompt tokens: "
        f"`{report['token_envelope']['ordinary_cap_saturated_prompt_tokens']}`\n"
        f"- Largest cap-saturated B1 request: "
        f"`{max(row['cap_saturated_b1_prompt_tokens']['max'] for row in chapter_rows)}` "
        f"of `{config.b1_input_cap}` tokens\n"
        f"- Legacy separate candidate-context bytes: "
        f"`{report['candidate_envelope']['legacy_separate_context_bytes']['sum']}`\n"
        f"- Prejoined candidate-context bytes: "
        f"`{report['candidate_envelope']['prejoined_context_bytes']['sum']}` "
        f"(`{report['candidate_envelope']['prejoined_to_legacy_context_ratio']}` of legacy)\n"
        f"- B1 configured cost upper bound: "
        f"`${report['configured_cost_upper_bound_usd']['b1']}`\n"
        f"- B0/Auditor monetary bounds: `unknown (null)`\n"
        f"- API calls: `0`\n"
        f"- Phase C ready: `false`\n\n"
        "The user waived a second cost-approval step for the two-chapter pilot. Phase C remains\n"
        "blocked on provider limits and a gated real-execution adapter. Completion figures are hard\n"
        "caps, not expected usage; the saturated-card scenario measures context overhead only.\n"
    )
    (output / "README.md").write_text(summary, encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-render M4f B0/B1 registry v2 for WH 1-2")
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = dry_render(
        document_path=args.document,
        design_doc=args.design_doc,
        output_dir=args.output_dir,
    )
    print(canonical_json(
        {
            "report_hash": report["report_hash"],
            "run_config_hash": report["run_config_hash"],
            "api_calls": report["api_calls"],
            "phase_c_ready": report["phase_c_ready"],
        }
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
