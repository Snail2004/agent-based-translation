from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pipeline.agents.llm_client import LLMResult
from pipeline.prepass.prompt import build_messages, short_block_id
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
    from_cache: bool
    system_fingerprint: str | None
    counts: dict[str, int]
    errors: list[str]


@dataclass(frozen=True)
class PrepassReport:
    document: str
    chapters_requested: list[str]
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
) -> PrepassReport:
    document_path = Path(document_json_path)
    document = json.loads(document_path.read_text(encoding="utf-8"))
    selected_chapters = _select_chapters(document, chapter_ids)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = PrepassRegistry()
    chapter_reports: list[ChapterRunReport] = []
    failed = 0
    all_results: list[LLMResult] = []

    for chapter in selected_chapters:
        chapter_id = str(chapter["chapter_id"])
        registry_text = registry.compress()
        messages = build_messages(chapter, registry_text)
        final_obj: dict[str, Any] | None = None
        errors: list[str] = []
        results: list[LLMResult] = []

        for attempt in range(2):
            result = client.call(
                messages,
                response_format={"type": "json_object"},
                tag=f"prepass_{chapter_id}",
            )
            results.append(result)
            all_results.append(result)
            parsed = result.parsed_json
            if parsed is None:
                errors = [f"JSON parse failed: {result.json_error or 'unknown error'}"]
            else:
                normalized = normalize_output_block_ids(parsed, chapter)
                errors = validate_chapter_output(
                    normalized,
                    expected_chapter_id=chapter_id,
                    known_entity_ids=registry.entity_ids,
                    valid_block_ids=_full_block_ids(chapter),
                )
                if not errors:
                    final_obj = normalized
                    break

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
        document=str(document.get("doc_id") or document_path.stem),
        chapters_requested=chapter_ids,
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
            result.usage.prompt_tokens for result in results if not result.from_cache
        ),
        completion_tokens=sum(
            result.usage.completion_tokens for result in results if not result.from_cache
        ),
        reasoning_tokens=sum(
            result.usage.reasoning_tokens for result in results if not result.from_cache
        ),
        cost_usd=round(
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
    }


def _total_usage(results: list[LLMResult]) -> dict[str, int | float]:
    return {
        "prompt_tokens": sum(
            result.usage.prompt_tokens for result in results if not result.from_cache
        ),
        "completion_tokens": sum(
            result.usage.completion_tokens for result in results if not result.from_cache
        ),
        "reasoning_tokens": sum(
            result.usage.reasoning_tokens for result in results if not result.from_cache
        ),
        "cost_usd": round(
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
