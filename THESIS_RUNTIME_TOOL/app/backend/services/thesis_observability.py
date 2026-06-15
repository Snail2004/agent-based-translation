from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from config import THESIS_JOBS_ROOT
from services.thesis_readmodel import ThesisReadModelError


JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_job_id(job_id: str) -> str:
    value = (job_id or "").strip()
    if not value or not JOB_ID_RE.match(value):
        raise ThesisReadModelError("invalid_job_id", "Invalid thesis job id.", 400)
    return value


def _job_dir(job_id: str, jobs_root: Path | None = None) -> Path:
    safe_job_id = _safe_job_id(job_id)
    root = (jobs_root or THESIS_JOBS_ROOT).resolve()
    path = (root / safe_job_id).resolve()
    if root not in path.parents:
        raise ThesisReadModelError("invalid_job_path", "Resolved job path escapes jobs root.", 400)
    if not path.exists():
        raise ThesisReadModelError("job_not_found", f"Job directory not found for {safe_job_id}.", 404)
    return path


def _memory_db_path(job_id: str, jobs_root: Path | None = None) -> Path:
    path = _job_dir(job_id, jobs_root) / "memory.sqlite3"
    if not path.exists():
        raise ThesisReadModelError("job_not_found", f"memory.sqlite3 not found for job {job_id}.", 404)
    return path


def _connect_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(con, table):
        return set()
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _list_like(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [{"key": key, "value": item} for key, item in value.items()]
    return [value]


def _memory_pack_context_audit(pack: dict[str, Any]) -> dict[str, Any]:
    payload = pack.get("payload") or {}
    debug = pack.get("retrieval_debug") or {}
    context_pack = payload.get("context_pack") if isinstance(payload, dict) else {}
    if not isinstance(context_pack, dict):
        context_pack = {}

    included = _list_like(debug.get("included"))
    if not included and isinstance(payload, dict):
        included = _list_like(payload.get("included"))
    if not included:
        included = _list_like(context_pack.get("included"))
    if not included:
        included = (
            _list_like(context_pack.get("glossary_lines"))
            + _list_like(context_pack.get("entity_lines"))
            + _list_like(context_pack.get("address_lines"))
        )

    excluded = _list_like(debug.get("excluded"))
    if not excluded and isinstance(payload, dict):
        excluded = _list_like(payload.get("excluded"))
    if not excluded:
        excluded = _list_like(context_pack.get("excluded"))

    dropped = _list_like(debug.get("dropped_by_budget"))
    if not dropped:
        dropped = _list_like(debug.get("dropped"))
    if not dropped and isinstance(payload, dict):
        dropped = _list_like(payload.get("dropped_by_budget"))
    if not dropped:
        dropped = _list_like(context_pack.get("dropped_by_budget"))

    anchors_count = {}
    if isinstance(payload, dict):
        anchors_count = payload.get("anchors_count") or {}
    if not anchors_count:
        anchors_count = context_pack.get("anchors_count") or {}

    return {
        "included_count": len(included),
        "excluded_count": len(excluded),
        "dropped_by_budget_count": len(dropped),
        "included_sample": included[:8],
        "excluded_sample": excluded[:8],
        "dropped_by_budget_sample": dropped[:8],
        "anchors_count": anchors_count,
        "source": "retrieval_debug_json|payload_json.context_pack",
    }


def _text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _usage(value: Any) -> dict[str, int]:
    raw = _json_load(value, {})
    prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    cached = int(raw.get("cached_tokens") or raw.get("cached_input_tokens") or 0)
    reasoning = int(raw.get("reasoning_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_quota_tokens": prompt + completion,
    }


def _messages(request_json: Any) -> list[dict[str, str]]:
    data = _json_load(request_json, {})
    result = []
    for message in data.get("messages") or []:
        result.append({
            "role": str(message.get("role") or ""),
            "content": _text_content(message.get("content")),
        })
    return result


def _message_stats(messages: list[dict[str, str]]) -> dict[str, int]:
    system_chars = sum(len(m["content"]) for m in messages if m.get("role") == "system")
    user_chars = sum(len(m["content"]) for m in messages if m.get("role") == "user")
    return {
        "message_count": len(messages),
        "system_chars": system_chars,
        "user_chars": user_chars,
        "total_chars": sum(len(m["content"]) for m in messages),
    }


def _infer_agent(source: str, tag: str) -> str:
    if source == "judge":
        return "Judge"
    if tag.startswith("prepass_"):
        return "Builder"
    if re.match(r"^S\d", tag or ""):
        return "Translator"
    return "LLM"


def _cache_status(usage: dict[str, int], cache_key: str) -> dict[str, Any]:
    prompt = usage["prompt_tokens"]
    cached = usage["cached_tokens"]
    return {
        "local_replay": {
            "stored_result": True,
            "cache_key": cache_key,
            "hit_events_logged": False,
            "note": "Row is replayable cache material; individual replay-hit events are not logged yet.",
        },
        "provider": {
            "cached_tokens": cached,
            "cache_hit": cached > 0,
            "cached_ratio": (cached / prompt) if prompt else 0.0,
        },
        "quota": {
            "counts_cached_input_fully": True,
            "total_quota_tokens": usage["total_quota_tokens"],
        },
    }


def _cache_db_specs(jobs_root: Path, job_dir: Path) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for path in sorted(jobs_root.glob("prepass_cache*.sqlite3")):
        specs.append({"source": "prepass", "table": "llm_call_cache", "path": path})
    for path in sorted(jobs_root.glob("translate_cache*.sqlite3")):
        specs.append({"source": "translate", "table": "llm_call_cache", "path": path})
    for path in sorted(job_dir.glob("judge*.sqlite3")):
        specs.append({"source": "judge", "table": "judge_call_cache", "path": path})
    return specs


def _document_id(memory_con: sqlite3.Connection, job_id: str) -> str:
    if not _table_exists(memory_con, "documents"):
        return job_id
    row = memory_con.execute("SELECT doc_id FROM documents LIMIT 1").fetchone()
    return str(row["doc_id"]) if row and row["doc_id"] else job_id


def _translation_context(memory_con: sqlite3.Connection) -> dict[str, Any]:
    tags: dict[str, dict[str, Any]] = {}
    if not _table_exists(memory_con, "translation_runs"):
        return {"tags": tags, "packs": {}, "runs_by_tag": {}}

    rows = [
        dict(row)
        for row in memory_con.execute(
            """
            SELECT run_id, experiment_id, doc_id, block_id, config, stage, window_id,
                   pack_id, model, prompt_version, temperature, seed,
                   cost, latency_ms, created_at
            FROM translation_runs
            ORDER BY created_at, config, window_id, block_id
            """
        ).fetchall()
    ]
    runs_by_tag: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        config = row.get("config") or ""
        window_id = row.get("window_id") or ""
        if not config or not window_id:
            continue
        tag = f"{config}_{window_id}"
        runs_by_tag[tag].append(row)

    packs = _packs_by_id(memory_con)
    for tag, tag_rows in runs_by_tag.items():
        first = tag_rows[0]
        pack_id = first.get("pack_id")
        pack = packs.get(pack_id or "")
        tags[tag] = {
            "tag": tag,
            "config": first.get("config"),
            "window_id": first.get("window_id"),
            "pack_id": pack_id,
            "pack_linked": pack is not None,
            "block_ids": [row.get("block_id") for row in tag_rows if row.get("block_id")],
            "block_count": len(tag_rows),
            "experiment_id": first.get("experiment_id"),
            "stage": first.get("stage"),
            "prompt_version": first.get("prompt_version"),
            "model": first.get("model"),
            "seed": first.get("seed"),
            "temperature": first.get("temperature"),
        }
    return {"tags": tags, "packs": packs, "runs_by_tag": dict(runs_by_tag)}


def _packs_by_id(memory_con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not _table_exists(memory_con, "memory_packs"):
        return {}
    result = {}
    for row in memory_con.execute("SELECT * FROM memory_packs").fetchall():
        item = dict(row)
        item["payload"] = _json_load(item.get("payload_json"), {})
        item["memory_refs"] = _json_load(item.get("memory_refs_json"), [])
        item["retrieval_debug"] = _json_load(item.get("retrieval_debug_json"), {})
        result[item["pack_id"]] = item
    return result


def _prepass_prefixes(doc_id: str, job_id: str) -> tuple[str, ...]:
    values = {doc_id, job_id}
    if doc_id == "d2l" or job_id.startswith("d2l"):
        values.add("d2l")
    if doc_id == "treasure_island" or job_id.startswith("treasure_island"):
        values.add("treasure_island")
    return tuple(f"prepass_{value}" for value in values if value)


def _row_to_call(
    row: sqlite3.Row,
    *,
    source: str,
    source_db: Path,
    table: str,
    translation_tags: dict[str, dict[str, Any]],
    include_detail: bool = False,
) -> dict[str, Any]:
    raw = dict(row)
    request = _json_load(raw.get("request_json"), {})
    messages = _messages(raw.get("request_json"))
    usage = _usage(raw.get("usage_json"))
    tag = raw.get("tag") or ""
    cache_key = raw.get("cache_key") or ""
    call_id = f"{source}:{cache_key}"
    linked = translation_tags.get(tag)
    request_params = {
        key: request.get(key)
        for key in ("model", "temperature", "seed", "reasoning_effort", "response_format", "base_url")
        if key in request
    }
    call = {
        "call_id": call_id,
        "cache_key": cache_key,
        "source": source,
        "source_table": table,
        "source_db": str(source_db),
        "agent": _infer_agent(source, tag),
        "tag": tag,
        "model": raw.get("model") or request.get("model"),
        "prompt_version": linked.get("prompt_version") if linked else None,
        "created_at": raw.get("created_at"),
        "latency_ms": raw.get("latency_ms") or 0,
        "cost_usd": float(raw.get("cost_usd") or 0.0),
        "usage": usage,
        "cache": _cache_status(usage, cache_key),
        "request_params": request_params,
        "message_stats": _message_stats(messages),
        "link": linked or {"pack_linked": False},
        "read_only": True,
    }
    if include_detail:
        call["messages"] = messages
        call["response_text"] = raw.get("response_text") or ""
    return call


def _load_cache_rows(
    spec: dict[str, Any],
    *,
    doc_id: str,
    job_id: str,
    translation_tags: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    path: Path = spec["path"]
    if not path.exists():
        return []
    try:
        with _connect_readonly(path) as con:
            table = spec["table"]
            if not _table_exists(con, table):
                return []
            rows = con.execute(f"SELECT * FROM {table} ORDER BY created_at, tag, cache_key").fetchall()
    except sqlite3.Error:
        return []

    prefixes = _prepass_prefixes(doc_id, job_id)
    calls = []
    for row in rows:
        tag = row["tag"] or ""
        source = spec["source"]
        include = (
            source == "judge"
            or (source == "translate" and tag in translation_tags)
            or (source == "prepass" and tag.startswith(prefixes))
        )
        if not include:
            continue
        calls.append(
            _row_to_call(
                row,
                source=source,
                source_db=path,
                table=spec["table"],
                translation_tags=translation_tags,
                include_detail=False,
            )
        )
    return calls


def _load_call_detail_from_spec(
    spec: dict[str, Any],
    *,
    cache_key: str,
    translation_tags: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    path: Path = spec["path"]
    if not path.exists():
        return None
    try:
        with _connect_readonly(path) as con:
            table = spec["table"]
            if not _table_exists(con, table):
                return None
            row = con.execute(f"SELECT * FROM {table} WHERE cache_key = ?", (cache_key,)).fetchone()
            if row is None:
                return None
            return _row_to_call(
                row,
                source=spec["source"],
                source_db=path,
                table=table,
                translation_tags=translation_tags,
                include_detail=True,
            )
    except sqlite3.Error:
        return None


def _usage_daily(specs: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cost_by_date: dict[str, float] = defaultdict(float)
    for call in calls:
        date = str(call.get("created_at") or "")[:10]
        if date:
            cost_by_date[date] += float(call.get("cost_usd") or 0.0)

    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for spec in specs:
        path: Path = spec["path"]
        if not path.exists():
            continue
        try:
            with _connect_readonly(path) as con:
                if not _table_exists(con, "usage_daily"):
                    continue
                has_cost = "cost_usd" in _columns(con, "usage_daily")
                select = "date, total_tokens, calls" + (", cost_usd" if has_cost else "")
                for row in con.execute(f"SELECT {select} FROM usage_daily ORDER BY date").fetchall():
                    key = (spec["source"], row["date"])
                    rows[key] = {
                        "source": spec["source"],
                        "source_db": str(path),
                        "date": row["date"],
                        "total_tokens": int(row["total_tokens"] or 0),
                        "calls": int(row["calls"] or 0),
                        "cost_usd": float(row["cost_usd"] or 0.0) if has_cost else None,
                        "cost_from_call_rows_usd": round(cost_by_date.get(row["date"], 0.0), 8),
                    }
        except sqlite3.Error:
            continue
    return sorted(rows.values(), key=lambda row: (row["date"], row["source"]))


def _totals(calls: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "calls": len(calls),
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "total_quota_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
    }
    by_agent: dict[str, dict[str, Any]] = {}
    by_config: dict[str, dict[str, Any]] = {}
    for call in calls:
        usage = call["usage"]
        for key in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "total_quota_tokens"):
            totals[key] += int(usage.get(key) or 0)
        totals["cost_usd"] += float(call.get("cost_usd") or 0.0)
        totals["latency_ms"] += int(call.get("latency_ms") or 0)
        agent = call.get("agent") or "LLM"
        item = by_agent.setdefault(agent, {"calls": 0, "total_quota_tokens": 0, "cost_usd": 0.0})
        item["calls"] += 1
        item["total_quota_tokens"] += int(usage.get("total_quota_tokens") or 0)
        item["cost_usd"] += float(call.get("cost_usd") or 0.0)
        config = call.get("link", {}).get("config")
        if config:
            cfg = by_config.setdefault(config, {"calls": 0, "total_quota_tokens": 0, "cost_usd": 0.0})
            cfg["calls"] += 1
            cfg["total_quota_tokens"] += int(usage.get("total_quota_tokens") or 0)
            cfg["cost_usd"] += float(call.get("cost_usd") or 0.0)
    totals["cost_usd"] = round(totals["cost_usd"], 8)
    for group in (by_agent, by_config):
        for item in group.values():
            item["cost_usd"] = round(item["cost_usd"], 8)
    return {"overall": totals, "by_agent": by_agent, "by_config": by_config}


def _attach_memory_pack(call: dict[str, Any], packs: dict[str, dict[str, Any]], runs_by_tag: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    link = call.get("link") or {}
    pack = packs.get(link.get("pack_id") or "")
    if pack:
        call["memory_pack"] = {
            "pack_id": pack.get("pack_id"),
            "pack_hash": pack.get("pack_hash"),
            "prompt_version": pack.get("prompt_version"),
            "estimated_tokens": pack.get("estimated_tokens") or 0,
            "payload": pack.get("payload"),
            "memory_refs": pack.get("memory_refs"),
            "retrieval_debug": pack.get("retrieval_debug"),
            "context_audit": _memory_pack_context_audit(pack),
            "created_at": pack.get("created_at"),
            "config": pack.get("config"),
            "source_table": "memory_packs",
        }
    else:
        call["memory_pack"] = None
    linked_runs = runs_by_tag.get(call.get("tag") or "", [])
    call["linked_runs"] = linked_runs[:100]
    if len(linked_runs) > 100:
        call["linked_runs_truncated"] = len(linked_runs) - 100

    usage = call["usage"]
    stats = call["message_stats"]
    system_est = round(stats["system_chars"] / 4)
    pack_est = int(call.get("memory_pack", {}).get("estimated_tokens") or 0) if call.get("memory_pack") else 0
    call["token_breakdown"] = {
        "actual_prompt_tokens": usage["prompt_tokens"],
        "actual_cached_tokens": usage["cached_tokens"],
        "actual_completion_tokens": usage["completion_tokens"],
        "estimated_system_tokens_from_chars": system_est,
        "estimated_memory_pack_tokens": pack_est,
        "estimated_other_prompt_tokens": max(0, usage["prompt_tokens"] - system_est - pack_est),
        "note": "System and other prompt sections are estimated from characters; memory_pack uses persisted estimated_tokens.",
    }
    return call


def load_observability(job_id: str, jobs_root: Path | None = None) -> dict[str, Any]:
    safe_job = _safe_job_id(job_id)
    root = (jobs_root or THESIS_JOBS_ROOT).resolve()
    job_dir = _job_dir(safe_job, root)
    memory_path = _memory_db_path(safe_job, root)
    with _connect_readonly(memory_path) as memory_con:
        doc_id = _document_id(memory_con, safe_job)
        context = _translation_context(memory_con)
    specs = _cache_db_specs(root, job_dir)
    calls: list[dict[str, Any]] = []
    for spec in specs:
        calls.extend(
            _load_cache_rows(
                spec,
                doc_id=doc_id,
                job_id=safe_job,
                translation_tags=context["tags"],
            )
        )
    calls.sort(key=lambda row: (row.get("created_at") or "", row.get("source") or "", row.get("tag") or ""), reverse=True)
    return {
        "meta": {
            "source": "thesis_observability_readmodel",
            "job_id": safe_job,
            "doc_id": doc_id,
            "read_only": True,
            "memory_db": str(memory_path),
            "cache_sources": [str(spec["path"]) for spec in specs if spec["path"].exists()],
            "known_gap": "llm_call_cache stores executed result rows; replay-hit events are not logged, so replay-hit rate requires future run-event logging.",
        },
        "calls": calls,
        "usage_daily": _usage_daily(specs, calls),
        "totals": _totals(calls),
    }


def load_observability_calls(job_id: str, jobs_root: Path | None = None) -> list[dict[str, Any]]:
    return load_observability(job_id, jobs_root)["calls"]


def load_call_detail(job_id: str, call_id: str, jobs_root: Path | None = None) -> dict[str, Any]:
    safe_job = _safe_job_id(job_id)
    if ":" not in call_id:
        raise ThesisReadModelError("invalid_call_id", "Call id must include source prefix.", 400)
    source, cache_key = call_id.split(":", 1)
    root = (jobs_root or THESIS_JOBS_ROOT).resolve()
    job_dir = _job_dir(safe_job, root)
    memory_path = _memory_db_path(safe_job, root)
    with _connect_readonly(memory_path) as memory_con:
        context = _translation_context(memory_con)
        packs = context["packs"]
        runs_by_tag = context["runs_by_tag"]

    specs = [spec for spec in _cache_db_specs(root, job_dir) if spec["source"] == source]
    for spec in specs:
        call = _load_call_detail_from_spec(
            spec,
            cache_key=cache_key,
            translation_tags=context["tags"],
        )
        if call is None:
            continue
        return _attach_memory_pack(call, packs, runs_by_tag)
    raise ThesisReadModelError("call_not_found", f"Call {call_id} not found for job {safe_job}.", 404)
