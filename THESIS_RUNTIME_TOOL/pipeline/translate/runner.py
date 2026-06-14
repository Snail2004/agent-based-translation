from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from pipeline.agents.llm_client import LLMResult
from pipeline.translate.prompt import (
    build_messages,
    extract_translations,
    prompt_version_for_config,
)
from pipeline.translate.profiles import get_profile


@dataclass(frozen=True)
class WindowRunReport:
    window_id: str
    status: str
    calls: int
    block_count: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    incremental_cost_usd: float
    from_cache: bool
    system_fingerprint: str | None
    errors: list[str]


@dataclass(frozen=True)
class TranslateReport:
    experiment_id: str
    config: str
    windows_total: int
    windows_translated: int
    windows_failed: int
    windows_skipped: int
    blocks_translated: int
    blocks_failed: int
    json_fail_rate: float
    total_usage: dict[str, int | float]
    context_stats: dict[str, int]
    model: str
    seed: int
    system_fingerprint: str | None
    reports: list[WindowRunReport]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "config": self.config,
            "windows_total": self.windows_total,
            "windows_translated": self.windows_translated,
            "windows_failed": self.windows_failed,
            "windows_skipped": self.windows_skipped,
            "blocks_translated": self.blocks_translated,
            "blocks_failed": self.blocks_failed,
            "json_fail_rate": self.json_fail_rate,
            "total_usage": self.total_usage,
            "context_stats": self.context_stats,
            "model": self.model,
            "seed": self.seed,
            "system_fingerprint": self.system_fingerprint,
            "reports": [asdict(r) for r in self.reports],
        }


def translate_windows(
    db: sqlite3.Connection,
    windows: list,
    client: Any,
    experiment_id: str,
    config: str = "S0",
    context_builder: Any | None = None,
    context_budget_tokens: int = 500,
    profile_name: str = "literary_v1",
) -> TranslateReport:
    """Run translation over a list of Window objects.

    Persists to translation_runs (1 row/block) and memory_packs (1 row/window).
    Resume: windows where every block already has a draft run are skipped.
    """

    reports: list[WindowRunReport] = []
    translated = 0
    failed = 0
    skipped = 0
    all_results: list[LLMResult] = []
    context_stats = {
        "windows_with_context": 0,
        "windows_low_context": 0,
        "dropped_by_budget": 0,
    }
    config = config.upper()
    profile = get_profile(profile_name)
    prompt_version = prompt_version_for_config(config, profile.name)

    for window in windows:
        window_id = window.window_id
        block_ids = list(window.block_ids)

        # --- Resume check ---
        already_done = _blocks_already_run(db, experiment_id, config, block_ids)
        if already_done == len(block_ids):
            reports.append(
                WindowRunReport(
                    window_id=window_id,
                    status="skipped",
                    calls=0,
                    block_count=len(block_ids),
                    prompt_tokens=0,
                    completion_tokens=0,
                    reasoning_tokens=0,
                    cost_usd=0.0,
                    incremental_cost_usd=0.0,
                    from_cache=False,
                    system_fingerprint=None,
                    errors=[],
                )
            )
            skipped += 1
            continue

        # --- Fetch block data ---
        block_rows = _fetch_blocks(db, block_ids)
        block_map = {str(row["block_id"]): dict(row) for row in block_rows}
        blocks_for_prompt = [block_map[bid] for bid in block_ids if bid in block_map]

        context_pack = None
        if config == "S1":
            context_pack = _build_context_pack_for_window(
                db,
                window,
                blocks_for_prompt,
                context_builder=context_builder,
                budget_tokens=context_budget_tokens,
                profile_name=profile.name,
            )
            context_stats["windows_with_context"] += 1
            if bool(getattr(context_pack, "low_context", False)):
                context_stats["windows_low_context"] += 1
            context_stats["dropped_by_budget"] += len(
                getattr(context_pack, "dropped_by_budget", []) or []
            )

        messages = build_messages(
            blocks_for_prompt,
            prompt_version=prompt_version,
            config=config,
            context_pack=context_pack,
            profile_name=profile.name,
        )

        # --- Call with re-ask ---
        result, status, errors = _call_with_reask(
            client, messages, window_id, block_ids, config
        )
        all_results.append(result)

        if status == "failed":
            failed += 1
        else:
            translated += 1

        # --- Persist ---
        model_name = str(getattr(client.config, "model", "") if hasattr(client, "config") else "")
        temperature = float(getattr(client.config, "temperature", 0.3) if hasattr(client, "config") else 0.3)
        seed = int(getattr(client.config, "seed", 0) if hasattr(client, "config") else 0)

        pack_id = f"pk_{config}_{window_id}"
        _persist_pack(
            db,
            pack_id,
            window_id,
            block_ids,
            config,
            messages,
            result,
            prompt_version=prompt_version,
            context_pack=context_pack,
            blocks_for_prompt=blocks_for_prompt,
            profile_name=profile.name,
        )

        if status == "translated":
            translations, _ = extract_translations(result.parsed_json, block_ids)
            for block_id, translation in translations.items():
                run_id = f"tr_{config}_{block_id}"
                _persist_run(
                    db, run_id, experiment_id, block_id, config, "draft",
                    window_id, pack_id, translation,
                    model_name, prompt_version, temperature, seed, result,
                )

        reports.append(
            WindowRunReport(
                window_id=window_id,
                status=status,
                calls=1,
                block_count=len(block_ids),
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                reasoning_tokens=result.usage.reasoning_tokens,
                cost_usd=result.cost_usd,
                incremental_cost_usd=result.cost_usd if not result.from_cache else 0.0,
                from_cache=result.from_cache,
                system_fingerprint=result.system_fingerprint,
                errors=errors,
            )
        )

    total_windows = len(windows)
    model_name = str(getattr(client.config, "model", "") if hasattr(client, "config") else "")
    seed = int(getattr(client.config, "seed", 0) if hasattr(client, "config") else 0)

    report = TranslateReport(
        experiment_id=experiment_id,
        config=config,
        windows_total=total_windows,
        windows_translated=translated,
        windows_failed=failed,
        windows_skipped=skipped,
        blocks_translated=sum(r.block_count for r in reports if r.status == "translated"),
        blocks_failed=sum(r.block_count for r in reports if r.status == "failed"),
        json_fail_rate=failed / total_windows if total_windows else 0.0,
        total_usage=_total_usage(all_results),
        context_stats=context_stats,
        model=model_name,
        seed=seed,
        system_fingerprint=_last_fingerprint(all_results),
        reports=reports,
    )

    db.commit()
    return report


def _build_context_pack_for_window(
    db: sqlite3.Connection,
    window: Any,
    blocks_for_prompt: list[dict[str, Any]],
    *,
    context_builder: Any | None,
    budget_tokens: int,
    profile_name: str,
) -> Any:
    if context_builder is not None:
        return context_builder(db, window, blocks_for_prompt)

    from pipeline.retrieval.context_builder import build_context_pack, plan_anchors

    anchors = plan_anchors(db, blocks_for_prompt, profile_name=profile_name)
    return build_context_pack(db, window, anchors, budget_tokens=budget_tokens)


def _call_with_reask(
    client: Any,
    messages: list[dict],
    window_id: str,
    block_ids: list[str],
    config: str,
) -> tuple[LLMResult, str, list[str]]:
    """Call LLM; re-ask once on validation failure."""
    for attempt in range(2):
        result = client.call(
            messages,
            response_format={"type": "json_object"},
            tag=f"{config}_{window_id}",
        )
        translations, parse_errors = extract_translations(result.parsed_json, block_ids)

        if not parse_errors and len(translations) == len(block_ids):
            return result, "translated", []

        errors = list(parse_errors)

        if attempt == 0:
            errors_msg = "; ".join(parse_errors[:5])
            messages = [
                *messages,
                {"role": "assistant", "content": result.text},
                {
                    "role": "user",
                    "content": (
                        f"Output errors: {errors_msg}. "
                        "Return a valid JSON object with all block_ids as keys and "
                        "Vietnamese translations as string values. No extra keys."
                    ),
                },
            ]

    return result, "failed", errors


def _blocks_already_run(
    db: sqlite3.Connection,
    experiment_id: str,
    config: str,
    block_ids: list[str],
) -> int:
    if not block_ids:
        return 0
    placeholders = ",".join("?" * len(block_ids))
    row = db.execute(
        f"""
        SELECT COUNT(DISTINCT block_id) AS cnt
        FROM translation_runs
        WHERE experiment_id = ? AND config = ? AND stage = 'draft'
          AND block_id IN ({placeholders})
        """,
        [experiment_id, config] + block_ids,
    ).fetchone()
    return int(row["cnt"] if row else 0)


def _fetch_blocks(db: sqlite3.Connection, block_ids: list[str]) -> list:
    if not block_ids:
        return []
    placeholders = ",".join("?" * len(block_ids))
    return db.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE block_id IN ({placeholders})
        ORDER BY order_index
        """,
        block_ids,
    ).fetchall()


def _persist_pack(
    db: sqlite3.Connection,
    pack_id: str,
    window_id: str,
    block_ids: list[str],
    config: str,
    messages: list[dict],
    result: LLMResult,
    *,
    prompt_version: str,
    context_pack: Any | None,
    blocks_for_prompt: list[dict[str, Any]],
    profile_name: str,
) -> None:
    # Store window context in payload_json (existing column).
    # config is stored via _add_column_if_missing during migration 005.
    zones = _zone_estimates(messages, blocks_for_prompt, context_pack)
    payload = {
        "window_id": window_id,
        "block_ids": block_ids,
        "config": config,
        "zones": zones,
        "prompt_version": prompt_version,
        "profile": profile_name,
        "anchors_count": _context_anchors_count(context_pack),
        "dropped_by_budget": _context_dropped_by_budget(context_pack),
        "low_context": bool(getattr(context_pack, "low_context", False))
        if context_pack is not None
        else False,
    }
    if context_pack is not None and hasattr(context_pack, "to_dict"):
        payload["context_pack"] = context_pack.to_dict()
    pack_hash = sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]

    existing = db.execute(
        "SELECT pack_id FROM memory_packs WHERE pack_id = ?", (pack_id,)
    ).fetchone()
    if existing:
        return

    doc_id = ""
    first_block = ""
    if block_ids:
        row = db.execute(
            "SELECT doc_id, block_id FROM blocks WHERE block_id = ?", (block_ids[0],)
        ).fetchone()
        if row:
            doc_id = str(row["doc_id"])
            first_block = str(row["block_id"])

    db.execute(
        """
        INSERT INTO memory_packs (
          pack_id, doc_id, block_id, pack_hash,
          prompt_version, estimated_tokens, payload_json, config
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pack_id,
            doc_id,
            first_block,
            pack_hash,
            prompt_version,
            result.usage.prompt_tokens + result.usage.completion_tokens,
            json.dumps(payload, ensure_ascii=False),
            config,
        ),
    )


def _persist_run(
    db: sqlite3.Connection,
    run_id: str,
    experiment_id: str,
    block_id: str,
    config: str,
    stage: str,
    window_id: str,
    pack_id: str,
    output_text: str,
    model: str,
    prompt_version: str,
    temperature: float,
    seed: int,
    result: LLMResult,
) -> None:
    row = db.execute(
        "SELECT doc_id FROM blocks WHERE block_id = ?", (block_id,)
    ).fetchone()
    doc_id = str(row["doc_id"]) if row else ""

    db.execute(
        """
        INSERT OR REPLACE INTO translation_runs (
          run_id, experiment_id, doc_id, block_id, config, stage,
          window_id, pack_id, output_text, model,
          prompt_version, temperature, seed,
          system_fingerprint, cost, latency_ms
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, experiment_id, doc_id, block_id, config, stage,
            window_id, pack_id, output_text, model,
            prompt_version, temperature, seed,
            result.system_fingerprint, result.cost_usd, result.latency_ms,
        ),
    )


def _total_usage(results: list[LLMResult]) -> dict[str, int | float]:
    return {
        "prompt_tokens": sum(r.usage.prompt_tokens for r in results),
        "completion_tokens": sum(r.usage.completion_tokens for r in results),
        "reasoning_tokens": sum(r.usage.reasoning_tokens for r in results),
        "cost_usd": round(sum(r.cost_usd for r in results), 12),
        "incremental_cost_usd": round(
            sum(r.cost_usd for r in results if not r.from_cache), 12
        ),
        "calls": len(results),
        "cache_hits": sum(1 for r in results if r.from_cache),
    }


def _last_fingerprint(results: list[LLMResult]) -> str | None:
    for result in reversed(results):
        if result.system_fingerprint:
            return result.system_fingerprint
    return None


def _zone_estimates(
    messages: list[dict],
    blocks_for_prompt: list[dict[str, Any]],
    context_pack: Any | None,
) -> dict[str, int]:
    system_text = str(messages[0].get("content", "")) if messages else ""
    hard_tokens = int(getattr(context_pack, "token_estimate", 0) or 0)
    source_text = "\n".join(
        str(block.get("clean_text") or block.get("source_text") or "")
        for block in blocks_for_prompt
    )
    return {
        "system_tokens": _estimate_tokens(system_text),
        "hard_constraints_tokens": hard_tokens,
        "source_tokens": _estimate_tokens(source_text),
    }


def _context_anchors_count(context_pack: Any | None) -> dict[str, int]:
    if context_pack is None:
        return {"terms": 0, "entities": 0, "address_policies": 0}
    anchors = getattr(context_pack, "anchors", None)
    count = getattr(anchors, "count_by_type", {"terms": 0, "entities": 0})
    return {
        "terms": int(count.get("terms", 0)),
        "entities": int(count.get("entities", 0)),
        "address_policies": len(getattr(context_pack, "address_lines", []) or []),
    }


def _context_dropped_by_budget(context_pack: Any | None) -> list[dict[str, str]]:
    if context_pack is None:
        return []
    result = []
    for item in getattr(context_pack, "dropped_by_budget", []) or []:
        if hasattr(item, "to_dict"):
            result.append(item.to_dict())
        else:
            result.append(dict(item))
    return result


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
