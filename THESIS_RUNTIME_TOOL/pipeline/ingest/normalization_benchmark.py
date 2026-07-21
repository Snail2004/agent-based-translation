from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pipeline.ingest.normalization_ir import AdapterResult, lexical_tokens, percentile
from pipeline.ingest.normalization_recommendation import recommend_benchmark_toolchain


AdapterRunner = Callable[[Path], AdapterResult]


@dataclass(frozen=True)
class SourceSpec:
    label: str
    path: Path


def _coverage(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def result_metrics(result: AdapterResult) -> dict[str, Any]:
    lengths = [len(block.text) for block in result.blocks]
    token_values = lexical_tokens(result.blocks)
    text_counts = Counter(block.text.casefold() for block in result.blocks)
    duplicate_blocks = sum(count - 1 for count in text_counts.values() if count > 1)
    kind_counts = Counter(block.kind for block in result.blocks)
    return {
        "output_sha256": result.output_sha256(),
        "duration_seconds": round(result.duration_seconds, 6),
        "units": len(result.units),
        "unit_kinds": dict(sorted(Counter(unit.unit_kind for unit in result.units).items())),
        "blocks": len(result.blocks),
        "block_kinds": dict(sorted(kind_counts.items())),
        "headings": kind_counts.get("heading", 0),
        "characters": sum(lengths),
        "lexical_tokens": len(token_values),
        "unique_tokens": len(set(token_values)),
        "duplicate_blocks": duplicate_blocks,
        "duplicate_block_rate": _coverage(duplicate_blocks, len(result.blocks)),
        "replacement_characters": sum(block.text.count("\ufffd") for block in result.blocks),
        "p95_block_characters": percentile(lengths, 0.95),
        "max_block_characters": max(lengths, default=0),
        "oversized_blocks_gt_4000": sum(length > 4000 for length in lengths),
        "structural_pointer_coverage": _coverage(
            sum(bool(block.source_ref) for block in result.blocks), len(result.blocks)
        ),
        "native_provenance_coverage": _coverage(
            sum(block.native_provenance for block in result.blocks), len(result.blocks)
        ),
        "document_unit_fallback": bool(result.units)
        and all(unit.unit_kind == "document_unit" for unit in result.units),
        "warnings": list(result.warnings),
    }


def observation_manifest(result: AdapterResult) -> dict[str, Any]:
    return {
        "adapter": result.adapter,
        "adapter_version": result.adapter_version,
        "source_path": result.source_path,
        "source_format": result.source_format,
        "output_sha256": result.output_sha256(),
        "units": [unit.stable_dict() for unit in result.units],
        "blocks": [
            {
                "ordinal": block.ordinal,
                "kind": block.kind,
                "heading_level": block.heading_level,
                "source_ref": block.source_ref,
                "native_provenance": block.native_provenance,
                "text_characters": len(block.text),
                "text_sha256": hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
            }
            for block in result.blocks
        ],
        "metadata": result.metadata,
        "warnings": list(result.warnings),
    }


def _shingles(tokens: Sequence[str], width: int = 5) -> set[tuple[str, ...]]:
    if len(tokens) < width:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[index : index + width]) for index in range(len(tokens) - width + 1)}


def pairwise_metrics(left: AdapterResult, right: AdapterResult) -> dict[str, Any]:
    left_tokens = lexical_tokens(left.blocks)
    right_tokens = lexical_tokens(right.blocks)
    left_counts = Counter(left_tokens)
    right_counts = Counter(right_tokens)
    shared_count = sum((left_counts & right_counts).values())
    left_shingles = _shingles(left_tokens)
    right_shingles = _shingles(right_tokens)
    shared_shingles = len(left_shingles & right_shingles)
    return {
        "left": left.adapter,
        "right": right.adapter,
        "token_coverage_left_by_right": _coverage(shared_count, len(left_tokens)),
        "token_coverage_right_by_left": _coverage(shared_count, len(right_tokens)),
        "ordered_shingle_coverage_left_by_right": _coverage(shared_shingles, len(left_shingles)),
        "ordered_shingle_coverage_right_by_left": _coverage(shared_shingles, len(right_shingles)),
        "unit_count_delta": len(right.units) - len(left.units),
        "heading_count_delta": (
            sum(block.kind == "heading" for block in right.blocks)
            - sum(block.kind == "heading" for block in left.blocks)
        ),
    }


def benchmark_source(
    source: SourceSpec,
    runners: dict[str, AdapterRunner],
    *,
    repeat: int = 1,
    include_manifest: bool = False,
) -> tuple[dict[str, Any], dict[str, AdapterResult]]:
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    results: dict[str, AdapterResult] = {}
    arms: dict[str, Any] = {}
    for name, runner in runners.items():
        attempts: list[AdapterResult] = []
        try:
            for _ in range(repeat):
                attempts.append(runner(source.path))
            hashes = [attempt.output_sha256() for attempt in attempts]
            result = attempts[0]
            results[name] = result
            arm_report = {
                "status": "ok",
                "adapter_version": result.adapter_version,
                "deterministic": len(set(hashes)) == 1,
                "repeat_hashes": hashes,
                "metrics": result_metrics(result),
            }
            if include_manifest:
                arm_report["manifest"] = observation_manifest(result)
            arms[name] = arm_report
        except Exception as exc:  # benchmark must report one failed arm without hiding others
            arms[name] = {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
    names = list(results)
    pairwise = [
        pairwise_metrics(results[names[left]], results[names[right]])
        for left in range(len(names))
        for right in range(left + 1, len(names))
    ]
    source_format = next((result.source_format for result in results.values()), None)
    source_report = {
        "label": source.label,
        "source_name": source.path.name,
        "source_sha256": hashlib.sha256(source.path.read_bytes()).hexdigest(),
        "source_format": source_format,
        "arms": arms,
        "pairwise": pairwise,
    }
    source_report["advisory_recommendation"] = recommend_benchmark_toolchain(source_report)
    return source_report, results


def benchmark_many(
    sources: Iterable[SourceSpec],
    runners: dict[str, AdapterRunner],
    *,
    repeat: int = 1,
    include_manifest: bool = False,
) -> dict[str, Any]:
    source_reports = []
    for source in sources:
        report, _ = benchmark_source(
            source,
            runners,
            repeat=repeat,
            include_manifest=include_manifest,
        )
        source_reports.append(report)
    return {
        "schema_version": "input-normalization-benchmark-v1",
        "repeat": repeat,
        "sources": source_reports,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Input Normalization Benchmark v1",
        "",
        f"Repeat count: {report.get('repeat', 1)}",
        "",
    ]
    for source in report.get("sources") or []:
        lines.extend(
            [
                f"## {source['label']}",
                "",
                f"Source: `{source['source_name']}` (`sha256:{source['source_sha256'][:12]}...`)",
                f"Format: `{source.get('source_format') or 'unknown'}`",
                "",
                "| Arm | Status | Version | Units | Blocks | Headings | Tokens | Native provenance | Seconds |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for name, arm in source.get("arms", {}).items():
            if arm.get("status") != "ok":
                lines.append(f"| {name} | error: {arm.get('error_type')} |  |  |  |  |  |  |  |")
                continue
            metrics = arm["metrics"]
            lines.append(
                "| {name} | ok | {version} | {units} | {blocks} | {headings} | {tokens} | {prov:.3f} | {seconds:.3f} |".format(
                    name=name,
                    version=arm.get("adapter_version", ""),
                    units=metrics["units"],
                    blocks=metrics["blocks"],
                    headings=metrics["headings"],
                    tokens=metrics["lexical_tokens"],
                    prov=metrics["native_provenance_coverage"],
                    seconds=metrics["duration_seconds"],
                )
            )
        if source.get("pairwise"):
            lines.extend(["", "Pairwise content overlap:", ""])
            for item in source["pairwise"]:
                lines.append(
                    "- {left} vs {right}: token coverage {lr:.3f}/{rl:.3f}, ordered shingles {ls:.3f}/{rs:.3f}.".format(
                        left=item["left"],
                        right=item["right"],
                        lr=item["token_coverage_left_by_right"],
                        rl=item["token_coverage_right_by_left"],
                        ls=item["ordered_shingle_coverage_left_by_right"],
                        rs=item["ordered_shingle_coverage_right_by_left"],
                    )
                )
        recommendation = source.get("advisory_recommendation") or {}
        if recommendation:
            lines.extend(
                [
                    "",
                    "Advisory recommendation:",
                    "",
                    f"- Primary: `{recommendation.get('primary') or 'none'}`.",
                    f"- Fallback: `{recommendation.get('fallback') or 'none'}`.",
                    f"- Structure: `{recommendation.get('structure_status')}`.",
                    f"- Manual review required: `{str(bool(recommendation.get('review_required'))).lower()}`.",
                ]
            )
            for reason in recommendation.get("reasons") or []:
                lines.append(f"- {reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: dict[str, Any], output_dir: str | Path) -> tuple[Path, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "benchmark.json"
    markdown_path = destination / "benchmark.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path
