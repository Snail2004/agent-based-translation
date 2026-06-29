from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.agents.llm_client import LLMClient, LLMResult, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig, load_llm_config
from pipeline.prepass.builder_v2_consolidate import (
    NOTEBOOK_STATUS_CONFLICT,
    Notebook,
    NotebookEntry,
    apply_builder_output,
    notebook_to_canonical_json,
)
from pipeline.prepass.builder_v2_render import (
    OUTPUT_TOKEN_CAP_ESTIMATE,
    PACK_TOKEN_CAP,
    PROMPT_TOKEN_CAP,
    PROMPT_VERSION,
    RESPONSE_FORMAT,
    _candidate_priority_sort,
    _find_entry_hits,
    _json_token_estimate,
    _number_variant_surfaces,
    _pack_item,
    build_builder_v2_messages,
    pack_json_text,
    prompt_text,
)
from pipeline.prepass.concept_key import concept_key
from pipeline.prepass.db_source import load_document_from_connection
from pipeline.prepass.runner import PrepassWindow, build_d2l_prepass_windows


DEFAULT_DB = Path("data/jobs/d2l_p1/memory.sqlite3")
DEFAULT_CONFIG = Path("pipeline/configs/llm_prepass.yaml")
DEFAULT_OUT = Path("data/reports/builder_v2_c2_pilot")
DEFAULT_C1_RENDER_REPORT = Path("data/reports/builder_v2_c1_render/builder_v2_b_render_report.json")
PACK_VERSION = "builder_v2_memory_pack_stage_c2_live_notebook"
PACK_PROVENANCE = "builder_v2_notebook"
REQUIRED_BUCKETS = ("new_terms", "updates_to_existing", "conflicts", "seen_existing_terms")


class ParseFailure(RuntimeError):
    pass


class CostGateExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Estimate:
    chapter_id: str
    calls: int
    estimated_prompt_tokens: int
    estimated_output_tokens_nominal: int
    estimated_output_tokens_cap: int
    estimated_total_tokens_nominal: int
    estimated_total_tokens_cap: int
    estimated_cost_usd_nominal: float
    estimated_cost_usd_cap: float
    pricing: dict[str, float]
    model_config: dict[str, Any]
    zero_api: bool = True
    source: str = "C1 render upper-bound, not empty-notebook simulation"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Builder v2 Stage C2 online pilot driver with estimate-only cost gate."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--doc-id", default="d2l")
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--c1-render-report", default=str(DEFAULT_C1_RENDER_REPORT))
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--confirm-usd", type=float)
    parser.add_argument("--cache-db")
    args = parser.parse_args()

    config = load_llm_config(args.config)
    db_path = Path(args.db)
    chapter, windows = load_chapter_windows(db_path, args.doc_id, args.chapter)
    estimate = estimate_pilot_cost(
        chapter_id=str(chapter["chapter_id"]),
        windows=windows,
        config=config,
        c1_render_report_path=Path(args.c1_render_report),
    )

    if args.estimate_only:
        print(json.dumps(estimate.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.confirm_usd is None:
        raise SystemExit("--confirm-usd is required for a real C2 pilot run")
    enforce_cost_gate(estimate, args.confirm_usd)
    _ensure_openai_key_available()

    out_dir = Path(args.out)
    cache_path = Path(args.cache_db) if args.cache_db else out_dir / "llm_cache.sqlite3"
    client = LLMClient(config, cache_path)
    report = run_online_pilot(
        db_path=db_path,
        doc_id=args.doc_id,
        chapter=chapter,
        windows=windows,
        client=client,
        config=config,
        out_dir=out_dir,
        estimate=estimate,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def load_chapter_windows(
    db_path: Path,
    doc_id: str,
    chapter_id: str,
) -> tuple[dict[str, Any], list[PrepassWindow]]:
    conn = _connect_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        document = load_document_from_connection(
            conn,
            doc_id,
            [chapter_id],
            translate_only=True,
        )
    finally:
        conn.close()
    chapter = document["chapters"][0]
    windows = build_d2l_prepass_windows(chapter)
    if not windows:
        raise ValueError(f"No Builder windows for chapter {chapter_id}")
    return chapter, windows


def estimate_pilot_cost(
    *,
    chapter_id: str,
    windows: list[PrepassWindow],
    config: LLMConfig,
    c1_render_report_path: Path = DEFAULT_C1_RENDER_REPORT,
) -> Estimate:
    prompt_tokens = _prompt_tokens_from_c1_report(c1_render_report_path, chapter_id)
    if prompt_tokens <= 0:
        prompt_tokens = len(windows) * PROMPT_TOKEN_CAP
    calls = len(windows)
    output_nominal = calls * OUTPUT_TOKEN_CAP_ESTIMATE
    output_cap = calls * config.max_output_tokens
    return Estimate(
        chapter_id=chapter_id,
        calls=calls,
        estimated_prompt_tokens=prompt_tokens,
        estimated_output_tokens_nominal=output_nominal,
        estimated_output_tokens_cap=output_cap,
        estimated_total_tokens_nominal=prompt_tokens + output_nominal,
        estimated_total_tokens_cap=prompt_tokens + output_cap,
        estimated_cost_usd_nominal=_cost(config, prompt_tokens, output_nominal),
        estimated_cost_usd_cap=_cost(config, prompt_tokens, output_cap),
        pricing=dict(config.pricing),
        model_config=config_to_dict(config),
    )


def enforce_cost_gate(estimate: Estimate, ceiling_usd: float) -> None:
    if estimate.estimated_cost_usd_cap > ceiling_usd:
        raise CostGateExceeded(
            f"Estimated cap cost ${estimate.estimated_cost_usd_cap:.4f} exceeds "
            f"--confirm-usd ${ceiling_usd:.4f}"
        )


def run_online_pilot(
    *,
    db_path: Path,
    doc_id: str,
    chapter: dict[str, Any],
    windows: list[PrepassWindow],
    client: Any,
    config: LLMConfig,
    out_dir: Path,
    estimate: Estimate,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    before_hash = _sha256(db_path)
    notebook = Notebook()
    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    block_types = {
        str(block["block_id"]): str(block.get("block_type") or "")
        for block in chapter.get("blocks", [])
    }
    per_window: list[dict[str, Any]] = []
    cost_log: list[dict[str, Any]] = []
    raw_outputs: list[dict[str, Any]] = []
    parse_failure_count = 0
    skipped_windows = 0

    for window in windows:
        pack, pack_audit = build_pack_from_notebook(notebook, window)
        messages = build_builder_v2_messages(
            pack=pack,
            chapter_id=window.chapter_id,
            window_id=window.window_id,
            blocks=window.blocks,
        )
        prompt_file = _write_prompt_archive(prompts_dir, window.window_id, messages)
        prompt_tokens = estimate_prompt_tokens(messages, RESPONSE_FORMAT)
        if pack_audit["pack_token_estimate"] > PACK_TOKEN_CAP:
            raise RuntimeError(
                f"{window.window_id}: pack token estimate {pack_audit['pack_token_estimate']} "
                f"exceeds cap {PACK_TOKEN_CAP}"
            )
        if prompt_tokens > PROMPT_TOKEN_CAP:
            raise RuntimeError(
                f"{window.window_id}: prompt token estimate {prompt_tokens} exceeds cap {PROMPT_TOKEN_CAP}"
            )

        parsed, attempts, parse_failures = call_and_parse_window(client, messages, window.window_id)
        result = attempts[-1] if attempts else None
        parse_failure_count += parse_failures
        status = "applied"
        if parsed is None:
            skipped_windows += 1
            status = "skipped_parse_failure"
        else:
            raw_outputs.append(
                {
                    "window_id": window.window_id,
                    "block_ids": [str(block["block_id"]) for block in window.blocks],
                    "parsed_output": parsed,
                }
            )
            apply_builder_output(
                notebook,
                parsed,
                window_id=window.window_id,
                block_types_by_id=block_types,
            )

        per_window.append(
            {
                "window_id": window.window_id,
                "block_ids": [str(block["block_id"]) for block in window.blocks],
                "status": status,
                "prompt_file": prompt_file.relative_to(out_dir).as_posix(),
                "pack_audit": pack_audit,
                "prompt_tokens_est": prompt_tokens,
                "cache_key": result.cache_key if result is not None else None,
                "from_cache": result.from_cache if result is not None else None,
                "parse_failures": parse_failures,
            }
        )
        for attempt_index, attempt in enumerate(attempts, start=1):
            cost_log.append(
                {
                    "window_id": window.window_id,
                    "attempt": attempt_index,
                    "from_cache": attempt.from_cache,
                    "prompt_tokens": attempt.usage.prompt_tokens,
                    "cached_tokens": attempt.usage.cached_tokens,
                    "completion_tokens": attempt.usage.completion_tokens,
                    "reasoning_tokens": attempt.usage.reasoning_tokens,
                    "cost_usd": attempt.cost_usd,
                    "latency_ms": attempt.latency_ms,
                    "cache_key": attempt.cache_key,
                }
            )

    after_hash = _sha256(db_path)
    if before_hash != after_hash:
        raise RuntimeError("Frozen DB hash changed during C2 pilot.")
    status = "degraded" if parse_failure_count else "passed"
    report = {
        "phase": "BUILDER-V2-C2-PILOT",
        "status": status,
        "doc_id": doc_id,
        "chapter_id": str(chapter["chapter_id"]),
        "prompt_version": PROMPT_VERSION,
        "db_path": str(db_path),
        "db_sha256": before_hash,
        "db_hash_unchanged": True,
        "zero_db_write": True,
        "zero_gold": True,
        "model_config": config_to_dict(config),
        "estimate": estimate.to_dict(),
        "summary": {
            "status": status,
            "windows": len(windows),
            "applied_windows": len(windows) - skipped_windows,
            "skipped_windows": skipped_windows,
            "parse_failure_count": parse_failure_count,
            "notebook_entries": len(notebook.entries),
            "rejected_stoplist": len(notebook.rejected_terms),
            "conflicts": sum(len(entry.conflict_ledger) for entry in notebook.entries.values()),
            "total_cost_usd": sum(float(item["cost_usd"]) for item in cost_log),
            "cache_hits": sum(1 for item in cost_log if item["from_cache"]),
            "cache_misses": sum(1 for item in cost_log if not item["from_cache"]),
        },
        "per_window_audit_file": "per_window_audit.json",
        "cost_log_file": "cost_log.json",
        "raw_outputs_file": "raw_outputs.json",
        "prompts_dir": "prompts",
        "notebook_file": "notebook.json",
        "decision_log_file": "decision_log.json",
    }

    (out_dir / "notebook.json").write_text(
        notebook_to_canonical_json(notebook) + "\n",
        encoding="utf-8",
    )
    (out_dir / "decision_log.json").write_text(
        json.dumps(
            [asdict(item) for item in notebook.decision_log],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (out_dir / "per_window_audit.json").write_text(
        json.dumps(per_window, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "cost_log.json").write_text(
        json.dumps(cost_log, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "raw_outputs.json").write_text(
        json.dumps(raw_outputs, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "builder_v2_c2_pilot_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def build_pack_from_notebook(
    notebook: Notebook,
    window: PrepassWindow,
    *,
    budget_tokens: int = PACK_TOKEN_CAP,
) -> tuple[dict[str, Any], dict[str, Any]]:
    text_by_block = {
        str(block["block_id"]): str(block.get("clean_text") or block.get("source_text") or "")
        for block in window.blocks
    }
    candidates: list[dict[str, Any]] = []
    excluded: list[str] = []
    detected_surfaces: set[str] = set()

    for entry in sorted(notebook.entries.values(), key=_entry_sort_key):
        exact_surface, exact_hits = _find_entry_surface_hits(entry, text_by_block)
        if exact_surface and exact_hits:
            detected_surfaces.add(exact_surface)
            candidates.append(_notebook_candidate(entry, "exact_surface", exact_surface, exact_hits))
            continue
        concept_surface, concept_hits = _find_number_variant_hits(entry, text_by_block)
        if concept_surface and concept_hits:
            detected_surfaces.add(concept_surface)
            candidates.append(_notebook_candidate(entry, "concept_key", concept_surface, concept_hits))
            continue
        excluded.append(entry.canonical_source_term)

    candidates.sort(key=_candidate_priority_sort)
    matched: list[dict[str, Any]] = []
    near_number: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for candidate in candidates:
        target = matched if candidate["match_type"] == "exact_surface" else near_number
        target.append(_pack_item(candidate))
        pack = _live_pack_payload(matched, near_number)
        token_estimate = _json_token_estimate(pack)
        if token_estimate > budget_tokens:
            target.pop()
            dropped.append(
                {
                    "glossary_id": candidate["glossary_id"],
                    "source_term": candidate["source_term"],
                    "match_type": candidate["match_type"],
                    "priority": candidate["priority"],
                    "reason": f"would_exceed_PACK_TOKEN_CAP_{budget_tokens}",
                }
            )

    pack = _live_pack_payload(matched, near_number)
    audit = {
        "included_by_exact_surface": [item["source_term"] for item in matched],
        "included_by_concept_key": [
            {
                "source_term": item["source_term"],
                "related_surface_seen": item["related_surface_seen"],
            }
            for item in near_number
        ],
        "excluded_no_surface_match": {
            "count": len(excluded),
            "sample": sorted(set(excluded), key=lambda value: (value.casefold(), value))[:30],
        },
        "dropped_by_budget": dropped,
        "pack_token_estimate": _json_token_estimate(pack),
        "window_term_surfaces_detected": sorted(
            detected_surfaces, key=lambda value: (value.casefold(), value)
        ),
        "pack_source_mode": "live_notebook",
        "pack_provenance": PACK_PROVENANCE,
    }
    return pack, audit


def call_and_parse_window(
    client: Any,
    messages: list[dict[str, Any]],
    window_id: str,
) -> tuple[dict[str, Any] | None, list[LLMResult], int]:
    first = client.call(
        messages,
        response_format=RESPONSE_FORMAT,
        tag=f"builder_v2_c2:{window_id}",
    )
    try:
        return parse_four_bucket_json(first), [first], 0
    except ParseFailure:
        second = client.call(
            messages,
            response_format=RESPONSE_FORMAT,
            tag=f"builder_v2_c2:{window_id}:reask",
            bypass_cache=True,
        )
        try:
            return parse_four_bucket_json(second), [first, second], 1
        except ParseFailure:
            return None, [first, second], 2


def parse_four_bucket_json(result: LLMResult | dict[str, Any]) -> dict[str, Any]:
    parsed: Any
    if isinstance(result, dict):
        parsed = result
    else:
        parsed = result.parsed_json
        if parsed is None:
            try:
                parsed = json.loads(result.text)
            except Exception as exc:
                raise ParseFailure(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ParseFailure("top-level response is not an object")
    missing = [key for key in REQUIRED_BUCKETS if key not in parsed]
    if missing:
        raise ParseFailure(f"missing required buckets: {missing}")
    for key in REQUIRED_BUCKETS:
        if not isinstance(parsed[key], list):
            raise ParseFailure(f"bucket {key} is not a list")
    return dict(parsed)


def config_to_dict(config: LLMConfig) -> dict[str, Any]:
    return {
        "model": config.model,
        "temperature": config.temperature,
        "seed": config.seed,
        "reasoning_effort": config.reasoning_effort,
        "verbosity": config.verbosity,
        "max_output_tokens": config.max_output_tokens,
        "daily_token_cap": config.daily_token_cap,
        "prompt_token_cap": config.prompt_token_cap,
        "pricing": dict(config.pricing),
    }


def _write_prompt_archive(
    prompts_dir: Path,
    window_id: str,
    messages: list[dict[str, Any]],
) -> Path:
    path = prompts_dir / f"{_safe_filename(window_id)}.txt"
    path.write_text(prompt_text(messages) + "\n", encoding="utf-8")
    return path


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)


def _prompt_tokens_from_c1_report(path: Path, chapter_id: str) -> int:
    if not path.exists():
        return 0
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    report_chapter = str(report.get("chapter_id") or "")
    if report_chapter and report_chapter != chapter_id:
        return 0
    windows = report.get("windows")
    if isinstance(windows, dict):
        return int(windows.get("total_prompt_tokens_est") or 0)
    return 0


def _cost(config: LLMConfig, prompt_tokens: int, completion_tokens: int) -> float:
    pricing = config.pricing
    return (
        (prompt_tokens / 1_000_000) * float(pricing["input"])
        + (completion_tokens / 1_000_000) * float(pricing["output"])
    )


def _find_entry_surface_hits(
    entry: NotebookEntry,
    text_by_block: dict[str, str],
) -> tuple[str | None, list[str]]:
    for surface in _entry_surfaces(entry):
        hits = _find_entry_hits(surface, text_by_block)
        if hits:
            return surface, hits
    return None, []


def _find_number_variant_hits(
    entry: NotebookEntry,
    text_by_block: dict[str, str],
) -> tuple[str | None, list[str]]:
    known = {surface.casefold() for surface in _entry_surfaces(entry)}
    for surface in _entry_surfaces(entry):
        for variant in _number_variant_surfaces(surface):
            if variant.casefold() in known:
                continue
            if concept_key(variant) != entry.concept_key:
                continue
            hits = _find_entry_hits(variant, text_by_block)
            if hits:
                return variant, hits
    return None, []


def _notebook_candidate(
    entry: NotebookEntry,
    match_type: str,
    surface_seen: str,
    evidence_block_ids: list[str],
) -> dict[str, Any]:
    return {
        "glossary_id": entry.concept_key,
        "source_term": entry.canonical_source_term,
        "canonical_target_vi": entry.canonical_target_vi,
        "allowed_variants": _target_variants(entry),
        "term_type": entry.term_type,
        "do_not_translate": entry.do_not_translate,
        "concept_key": entry.concept_key,
        "match_type": match_type,
        "related_surface_seen": surface_seen if match_type == "concept_key" else None,
        "evidence_block_ids": sorted(set(evidence_block_ids)),
        "status": "conflict_pending" if entry.status == NOTEBOOK_STATUS_CONFLICT else None,
        "occurrences_total": int(entry.occurrences_total or 0),
        "priority": None,
    }


def _entry_surfaces(entry: NotebookEntry) -> list[str]:
    values = [variant.surface for variant in entry.source_variants if variant.surface]
    if entry.canonical_source_term:
        values.append(entry.canonical_source_term)
    return sorted(set(values), key=lambda value: (value.casefold() != entry.canonical_source_term.casefold(), value.casefold(), value))


def _target_variants(entry: NotebookEntry) -> list[str]:
    seen = {entry.canonical_target_vi.casefold()} if entry.canonical_target_vi else set()
    values: list[str] = []
    for variant in entry.target_variants:
        text = str(variant.text or "").strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        values.append(text)
        if len(values) >= 2:
            break
    return values


def _entry_sort_key(entry: NotebookEntry) -> tuple[Any, ...]:
    return (
        entry.first_seen_window or "",
        entry.canonical_source_term.casefold(),
        entry.concept_key,
    )


def _live_pack_payload(
    matched_existing_terms: list[dict[str, Any]],
    near_number_variants: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "pack_version": PACK_VERSION,
        "pack_source_mode": "live_notebook",
        "pack_provenance": PACK_PROVENANCE,
        "matched_existing_terms": matched_existing_terms,
        "near_number_variants": near_number_variants,
    }


def _sha256(path: Path) -> str:
    return __import__("hashlib").sha256(path.read_bytes()).hexdigest().upper()


def _ensure_openai_key_available() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    key_paths = [
        Path("OPENAI_API_KEY.txt"),
        Path("OPENAI-KEY-1.txt"),
        Path("OPENAI-KEY-2.txt"),
        Path("../OPENAI_API_KEY.txt"),
        Path("../OPENAI-KEY-1.txt"),
        Path("../OPENAI-KEY-2.txt"),
    ]
    for path in key_paths:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                os.environ["OPENAI_API_KEY"] = value
                return
    raise RuntimeError("OPENAI_API_KEY is required for real C2 pilot runs.")


def _safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)
    return safe.strip("_") or "window"


if __name__ == "__main__":
    raise SystemExit(main())
