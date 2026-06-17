from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.eval.thesis_scoring import normalize_apostrophe


CONSTRAINT_STRENGTHS = {
    "hard",
    "soft",
    "preserve",
    "entity",
    "ignore_for_consistency",
}


@dataclass(frozen=True)
class TermPolicyAssets:
    stoplist: set[str]
    hard_allowlist: set[str]
    overrides: dict[str, str]
    override_justifications: dict[str, str]
    fixes: list[dict[str, str]]
    paths: dict[str, str]


def classify_term(
    row: dict[str, Any],
    *,
    stoplist: set[str],
    hard_allowlist: set[str],
    overrides: dict[str, str],
) -> str:
    """Classify a registry row without looking at translation outputs."""

    source = _source_key(row.get("source_term") or "")
    if source in overrides:
        return overrides[source]

    term_type = str(row.get("term_type") or "").casefold()
    do_not_translate = bool(int(row.get("do_not_translate") or 0))

    if do_not_translate or term_type == "code_api":
        return "preserve"
    if term_type == "proper_noun":
        return "entity"
    if term_type == "abbreviation":
        return "soft"

    if _token_count(source) >= 2:
        return "hard"
    if source in stoplist:
        return "ignore_for_consistency"
    if source in hard_allowlist:
        return "hard"
    return "soft"


def load_term_policy_assets(root: str | Path) -> TermPolicyAssets:
    base = Path(root)
    stoplist_path = base / "d2l_term_stoplist.txt"
    hard_path = base / "d2l_term_hard_allowlist.txt"
    overrides_path = base / "d2l_term_policy_overrides.csv"
    fixes_path = base / "d2l_glossary_fixes.csv"

    overrides: dict[str, str] = {}
    override_justifications: dict[str, str] = {}
    if overrides_path.exists():
        with overrides_path.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                source = _source_key(row.get("source_term") or "")
                strength = str(row.get("constraint_strength") or "").strip()
                if not source:
                    continue
                _validate_strength(strength, source)
                overrides[source] = strength
                override_justifications[source] = str(row.get("justification") or "").strip()

    fixes: list[dict[str, str]] = []
    if fixes_path.exists():
        with fixes_path.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                source = _source_key(row.get("source_term") or "")
                op = str(row.get("op") or "").strip()
                value = str(row.get("value") or "").strip()
                justification = str(row.get("justification") or "").strip()
                if not source:
                    continue
                if op not in {"remove_variant", "set_canonical"}:
                    raise ValueError(f"Unknown glossary fix op for {source}: {op}")
                if not value:
                    raise ValueError(f"Glossary fix for {source} has empty value")
                fixes.append(
                    {
                        "source_term": source,
                        "op": op,
                        "value": value,
                        "justification": justification,
                    }
                )

    return TermPolicyAssets(
        stoplist=_load_source_set(stoplist_path),
        hard_allowlist=_load_source_set(hard_path),
        overrides=overrides,
        override_justifications=override_justifications,
        fixes=fixes,
        paths={
            "stoplist": str(stoplist_path),
            "hard_allowlist": str(hard_path),
            "overrides": str(overrides_path),
            "glossary_fixes": str(fixes_path),
        },
    )


def apply_glossary_fixes(
    rows: list[dict[str, Any]],
    fixes: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Apply eval-only glossary fixes to copies of registry rows."""

    by_source: dict[str, list[dict[str, Any]]] = {}
    result = [dict(row) for row in rows]
    for row in result:
        by_source.setdefault(_source_key(row.get("source_term") or ""), []).append(row)

    for fix in fixes:
        for row in by_source.get(fix["source_term"], []):
            if fix["op"] == "remove_variant":
                _remove_allowed_variant(row, fix["value"])
            elif fix["op"] == "set_canonical":
                _set_canonical(row, fix["value"])
    return result


def annotate_constraint_strength(
    rows: list[dict[str, Any]],
    assets: TermPolicyAssets,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        source = _source_key(item.get("source_term") or "")
        item["constraint_strength"] = classify_term(
            item,
            stoplist=assets.stoplist,
            hard_allowlist=assets.hard_allowlist,
            overrides=assets.overrides,
        )
        if source in assets.override_justifications:
            item["constraint_strength_justification"] = assets.override_justifications[source]
        result.append(item)
    return result


def _load_source_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    values: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw.split("#", 1)[0].strip()
        if text:
            values.add(_source_key(text))
    return values


def _remove_allowed_variant(row: dict[str, Any], value: str) -> None:
    variants = [
        item
        for item in _json_list(row.get("allowed_variants_json"))
        if _vi_key(item) != _vi_key(value)
    ]
    row["allowed_variants_json"] = json.dumps(variants, ensure_ascii=False)


def _set_canonical(row: dict[str, Any], value: str) -> None:
    old = str(row.get("target_term") or "").strip()
    variants = [str(item).strip() for item in _json_list(row.get("allowed_variants_json"))]
    if old:
        variants.append(old)
    variants.append(value)
    row["target_term"] = value
    row["allowed_variants_json"] = json.dumps(_dedupe_vi(variants), ensure_ascii=False)


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _dedupe_vi(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = _vi_key(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _validate_strength(value: str, source: str) -> None:
    if value not in CONSTRAINT_STRENGTHS:
        raise ValueError(f"Invalid constraint_strength for {source}: {value}")


def _token_count(source: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+", source))


def _source_key(value: Any) -> str:
    return unicodedata.normalize("NFC", normalize_apostrophe(str(value))).casefold().strip()


def _vi_key(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFC", normalize_apostrophe(str(value))).casefold().strip(),
    )
