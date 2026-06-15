from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pipeline.agents.llm_client import LLMResult
from pipeline.prepass.prompt import (
    D2L_REGISTRY_OMITTED_TEXT,
    D2L_TERMINOLOGY_PROMPT_VERSION,
    LITERARY_PROMPT_VERSION,
    build_messages,
    short_block_id,
)
from pipeline.prepass.literary_context import build_literary_builder_context_pack
from pipeline.prepass.registry import PrepassRegistry
from pipeline.prepass.schemas import validate_chapter_output


@dataclass(frozen=True)
class ChapterRunReport:
    chapter_id: str
    status: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    incremental_cost_usd: float
    from_cache: bool
    system_fingerprint: str | None
    counts: dict[str, int]
    errors: list[str]


@dataclass(frozen=True)
class PrepassWindow:
    window_id: str
    chapter_id: str
    blocks: list[dict[str, Any]]
    est_src_tokens: int


@dataclass(frozen=True)
class PrepassReport:
    document: str
    chapters_requested: list[str]
    mode: str
    prompt_version: str
    chapters: list[ChapterRunReport]
    json_fail_rate: float
    total_usage: dict[str, int | float]
    model: str
    seed: int
    system_fingerprint: str | None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "chapters_requested": self.chapters_requested,
            "mode": self.mode,
            "prompt_version": self.prompt_version,
            "chapters": [asdict(chapter) for chapter in self.chapters],
            "json_fail_rate": self.json_fail_rate,
            "total_usage": self.total_usage,
            "model": self.model,
            "seed": self.seed,
            "system_fingerprint": self.system_fingerprint,
        }


def run_prepass(
    document_json_path: str | Path,
    chapter_ids: list[str],
    client: Any,
    out_dir: str | Path,
    *,
    mode: str = "literary",
) -> PrepassReport:
    document_path = Path(document_json_path)
    document = json.loads(document_path.read_text(encoding="utf-8"))
    return run_prepass_document(
        document,
        chapter_ids,
        client,
        out_dir,
        document_label=str(document_path),
        mode=mode,
    )


def run_prepass_document(
    document: dict[str, Any],
    chapter_ids: list[str],
    client: Any,
    out_dir: str | Path,
    *,
    document_label: str = "db",
    mode: str = "literary",
    d2l_window_target_tokens: int = 500,
    d2l_window_max_blocks: int = 16,
) -> PrepassReport:
    selected_chapters = _select_chapters(document, chapter_ids)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = PrepassRegistry()
    chapter_reports: list[ChapterRunReport] = []
    failed = 0
    all_results: list[LLMResult] = []

    for chapter in selected_chapters:
        chapter_id = str(chapter["chapter_id"])
        if mode == "d2l_terminology":
            final_obj, results, errors = _run_d2l_windowed_chapter(
                chapter,
                registry,
                client,
                target_tokens=d2l_window_target_tokens,
                max_blocks=d2l_window_max_blocks,
            )
        else:
            final_obj, results, errors = _run_single_chapter(
                chapter,
                registry,
                client,
                mode=mode,
                tag=f"prepass_{chapter_id}",
            )
        all_results.extend(results)

        if final_obj is None:
            failed += 1
            chapter_reports.append(
                _chapter_report(chapter_id, "failed", results, {}, errors)
            )
            continue

        (output_dir / f"{chapter_id}.json").write_text(
            json.dumps(final_obj, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        registry.merge(final_obj)
        chapter_reports.append(
            _chapter_report(chapter_id, "passed", results, _counts(final_obj), [])
        )

    report = PrepassReport(
        document=str(document.get("doc_id") or document_label),
        chapters_requested=chapter_ids,
        mode=mode,
        prompt_version=_prompt_version(mode),
        chapters=chapter_reports,
        json_fail_rate=(failed / len(selected_chapters)) if selected_chapters else 0.0,
        total_usage=_total_usage(all_results),
        model=str(getattr(client.config, "model", "")),
        seed=int(getattr(client.config, "seed", 0)),
        system_fingerprint=_last_fingerprint(all_results),
    )
    (output_dir / "run_report.json").write_text(
        json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def normalize_output_block_ids(
    obj: dict[str, Any],
    chapter: dict[str, Any],
) -> dict[str, Any]:
    id_map = {short_block_id(str(block["block_id"])): str(block["block_id"]) for block in chapter.get("blocks") or []}
    id_map.update({str(block["block_id"]): str(block["block_id"]) for block in chapter.get("blocks") or []})
    normalized = json.loads(json.dumps(obj, ensure_ascii=False))
    for term in normalized.get("glossary_candidates") or []:
        if not isinstance(term, dict):
            continue
        if "block_ids" in term:
            term["block_ids"] = [_expand_block_id(item, id_map) for item in term.get("block_ids") or []]
        if "evidence_span_ids" in term:
            term["evidence_span_ids"] = [
                _expand_block_id(item, id_map)
                for item in term.get("evidence_span_ids") or []
            ]
    for relation in normalized.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        trigger = relation.get("trigger_block_id")
        if trigger is not None:
            relation["trigger_block_id"] = _expand_block_id(trigger, id_map)
    for motif in normalized.get("motifs") or []:
        if not isinstance(motif, dict):
            continue
        if "block_ids" in motif:
            motif["block_ids"] = [_expand_block_id(item, id_map) for item in motif.get("block_ids") or []]
    return normalized


def normalize_d2l_terminology_output(
    obj: dict[str, Any],
    *,
    valid_block_ids: set[str] | None = None,
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(obj, ensure_ascii=False))
    terms: list[dict[str, Any]] = []
    for term in normalized.get("glossary_candidates") or []:
        if not isinstance(term, dict):
            continue
        if valid_block_ids is not None:
            term["block_ids"] = [
                block_id
                for block_id in term.get("block_ids") or []
                if isinstance(block_id, str) and block_id in valid_block_ids
            ]
            term["evidence_span_ids"] = [
                block_id
                for block_id in term.get("evidence_span_ids") or []
                if isinstance(block_id, str) and block_id in valid_block_ids
            ]
            if not term["block_ids"] or not term["evidence_span_ids"]:
                continue
        terms.append(term)
    normalized["glossary_candidates"] = terms
    normalized["entities"] = []
    normalized["relations"] = []
    normalized["mention_surfaces"] = []
    normalized["motifs"] = []
    return normalized


def build_d2l_prepass_windows(
    chapter: dict[str, Any],
    *,
    target_tokens: int = 500,
    max_blocks: int = 16,
) -> list[PrepassWindow]:
    blocks = sorted(
        [block for block in chapter.get("blocks") or [] if block.get("block_id")],
        key=lambda block: int(block.get("order_index") or 0),
    )
    if not blocks:
        return []
    chapter_id = str(chapter.get("chapter_id") or "")
    windows: list[PrepassWindow] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0

    for block in blocks:
        text = str(block.get("clean_text") or block.get("source_text") or "")
        block_tokens = _estimate_tokens(text)
        is_oversize = block_tokens > target_tokens
        would_overflow = current and (
            current_tokens + block_tokens > target_tokens
            or len(current) >= max_blocks
        )
        if would_overflow:
            windows.append(_make_prepass_window(chapter_id, len(windows) + 1, current, current_tokens))
            current = []
            current_tokens = 0

        if is_oversize:
            windows.append(_make_prepass_window(chapter_id, len(windows) + 1, [block], block_tokens))
            continue

        current.append(block)
        current_tokens += block_tokens

    if current:
        windows.append(_make_prepass_window(chapter_id, len(windows) + 1, current, current_tokens))
    return windows


def _run_d2l_windowed_chapter(
    chapter: dict[str, Any],
    registry: PrepassRegistry,
    client: Any,
    *,
    target_tokens: int,
    max_blocks: int,
) -> tuple[dict[str, Any] | None, list[LLMResult], list[str]]:
    chapter_id = str(chapter["chapter_id"])
    windows = build_d2l_prepass_windows(
        chapter,
        target_tokens=target_tokens,
        max_blocks=max_blocks,
    )
    results: list[LLMResult] = []
    errors: list[str] = []
    outputs: list[dict[str, Any]] = []

    for window in windows:
        window_chapter = {
            **chapter,
            "blocks": window.blocks,
            "window_id": window.window_id,
        }
        output, window_results, window_errors = _run_single_chapter(
            window_chapter,
            registry,
            client,
            mode="d2l_terminology",
            tag=f"prepass_{chapter_id}_{window.window_id}",
            registry_text=D2L_REGISTRY_OMITTED_TEXT,
            valid_block_ids={str(block["block_id"]) for block in window.blocks},
        )
        results.extend(window_results)
        if output is None:
            errors = [f"{window.window_id}: {error}" for error in window_errors]
            return None, results, errors
        output["window_id"] = window.window_id
        outputs.append(output)

    merged = _merge_d2l_window_outputs(chapter_id, outputs)
    merged["window_count"] = len(windows)
    merged["windows"] = [
        {
            "window_id": window.window_id,
            "block_ids": [str(block["block_id"]) for block in window.blocks],
            "est_src_tokens": window.est_src_tokens,
        }
        for window in windows
    ]
    return merged, results, []


def _run_single_chapter(
    chapter: dict[str, Any],
    registry: PrepassRegistry,
    client: Any,
    *,
    mode: str,
    tag: str,
    registry_text: str | None = None,
    valid_block_ids: set[str] | None = None,
) -> tuple[dict[str, Any] | None, list[LLMResult], list[str]]:
    chapter_id = str(chapter["chapter_id"])
    if registry_text is None and mode == "literary":
        context_pack = build_literary_builder_context_pack(chapter, registry)
        registry_context_text = context_pack.render_context()
    else:
        registry_context_text = registry_text or registry.compress()
    messages = build_messages(chapter, registry_context_text, mode=mode)
    errors: list[str] = []
    results: list[LLMResult] = []

    for attempt in range(2):
        result = client.call(
            messages,
            response_format={"type": "json_object"},
            tag=tag,
        )
        results.append(result)
        parsed = result.parsed_json
        if parsed is None:
            errors = [f"JSON parse failed: {result.json_error or 'unknown error'}"]
        else:
            normalized = normalize_output_block_ids(parsed, chapter)
            if mode == "d2l_terminology":
                normalized = normalize_d2l_terminology_output(
                    normalized,
                    valid_block_ids=valid_block_ids or _full_block_ids(chapter),
                )
            errors = validate_chapter_output(
                normalized,
                expected_chapter_id=chapter_id,
                known_entity_ids=registry.entity_ids,
                valid_block_ids=valid_block_ids or _full_block_ids(chapter),
                mode=mode,
            )
            if not errors:
                return normalized, results, []

        if attempt == 0:
            messages = [
                *messages,
                {"role": "assistant", "content": result.text},
                {
                    "role": "user",
                    "content": (
                        "Output truoc sai: "
                        + "; ".join(errors)
                        + ". Tra lai JSON dung schema, du field, khong them loi."
                    ),
                },
            ]

    return None, results, errors


def _merge_d2l_window_outputs(
    chapter_id: str,
    outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    summaries = [
        str(output.get("chapter_summary_vi") or "").strip()
        for output in outputs
        if str(output.get("chapter_summary_vi") or "").strip()
    ]
    return {
        "chapter_id": chapter_id,
        "glossary_candidates": [
            term
            for output in outputs
            for term in output.get("glossary_candidates") or []
            if isinstance(term, dict)
        ],
        "entities": [],
        "relations": [],
        "mention_surfaces": [],
        "chapter_summary_vi": " ".join(summaries)[:1200],
        "motifs": [],
    }


def _make_prepass_window(
    chapter_id: str,
    counter: int,
    blocks: list[dict[str, Any]],
    est_tokens: int,
) -> PrepassWindow:
    return PrepassWindow(
        window_id=f"wb_{chapter_id}_{counter:03d}",
        chapter_id=chapter_id,
        blocks=list(blocks),
        est_src_tokens=est_tokens,
    )


def _estimate_tokens(text: str) -> int:
    return max(1, len(str(text)) // 4)


def _select_chapters(document: dict[str, Any], chapter_ids: list[str]) -> list[dict[str, Any]]:
    chapters = document.get("chapters") or []
    selected: list[dict[str, Any]] = []
    for requested in chapter_ids:
        matches = [
            chapter
            for chapter in chapters
            if str(chapter.get("chapter_id") or "") == requested
            or str(chapter.get("chapter_id") or "").endswith(f"_{requested}")
        ]
        if not matches:
            raise ValueError(f"Chapter not found: {requested}")
        selected.append(matches[0])
    return selected


def _expand_block_id(block_id: Any, id_map: dict[str, str]) -> str:
    value = str(block_id)
    return id_map.get(value, value)


def _full_block_ids(chapter: dict[str, Any]) -> set[str]:
    return {str(block["block_id"]) for block in chapter.get("blocks") or []}


def _chapter_report(
    chapter_id: str,
    status: str,
    results: list[LLMResult],
    counts: dict[str, int],
    errors: list[str],
) -> ChapterRunReport:
    return ChapterRunReport(
        chapter_id=chapter_id,
        status=status,
        calls=len(results),
        prompt_tokens=sum(
            result.usage.prompt_tokens for result in results
        ),
        completion_tokens=sum(
            result.usage.completion_tokens for result in results
        ),
        reasoning_tokens=sum(
            result.usage.reasoning_tokens for result in results
        ),
        cost_usd=round(sum(result.cost_usd for result in results), 12),
        incremental_cost_usd=round(
            sum(result.cost_usd for result in results if not result.from_cache), 12
        ),
        from_cache=bool(results) and all(result.from_cache for result in results),
        system_fingerprint=_last_fingerprint(results),
        counts=counts,
        errors=errors,
    )


def _counts(obj: dict[str, Any]) -> dict[str, int]:
    return {
        "terms": len(obj.get("glossary_candidates") or []),
        "entities": len(obj.get("entities") or []),
        "relations": len(obj.get("relations") or []),
        "mentions": len(obj.get("mention_surfaces") or []),
        "motifs": len(obj.get("motifs") or []),
        "windows": int(obj.get("window_count") or 1),
    }


def _total_usage(results: list[LLMResult]) -> dict[str, int | float]:
    return {
        "prompt_tokens": sum(
            result.usage.prompt_tokens for result in results
        ),
        "completion_tokens": sum(
            result.usage.completion_tokens for result in results
        ),
        "reasoning_tokens": sum(
            result.usage.reasoning_tokens for result in results
        ),
        "cost_usd": round(sum(result.cost_usd for result in results), 12),
        "incremental_cost_usd": round(
            sum(result.cost_usd for result in results if not result.from_cache), 12
        ),
        "calls": len(results),
        "cache_hits": sum(1 for result in results if result.from_cache),
    }


def _last_fingerprint(results: list[LLMResult]) -> str | None:
    for result in reversed(results):
        if result.system_fingerprint:
            return result.system_fingerprint
    return None


def _prompt_version(mode: str) -> str:
    if mode == "d2l_terminology":
        return D2L_TERMINOLOGY_PROMPT_VERSION
    return LITERARY_PROMPT_VERSION
