from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from pipeline.prepass.concept_key import concept_key, normalize_phrase


NOTEBOOK_STATUS_OK = "ok"
NOTEBOOK_STATUS_CONFLICT = "conflict_pending"

DECISION_TYPES = {
    "created",
    "merged_by_concept_key",
    "updated_source_variant",
    "updated_target_variant",
    "rejected_stoplist",
    "conflict_logged",
    "seen_existing",
}

CONFLICT_STATUS_TYPES = {
    "canonical_target_change",
    "polysemy_suspected",
    "bad_existing_target",
}

STRONG_TERM_BLOCK_TYPES = {"heading", "definition", "math", "math_block"}

STOPLIST_SINGLE_TOKENS = {
    "case",
    "example",
    "input",
    "number",
    "output",
    "problem",
    "result",
    "sample",
    "set",
    "size",
    "step",
    "value",
}

ALLOWLIST_SINGLE_TOKENS = {
    "ablation",
    "activation",
    "bias",
    "corpus",
    "dropout",
    "gradient",
    "logit",
    "matrix",
    "minibatch",
    "perceptron",
    "scalar",
    "softmax",
    "tensor",
    "vector",
}

VI_PLURAL_MARKERS = ("các ", "những ")
VI_LIGHT_TOKENS = {
    "các",
    "cái",
    "cho",
    "của",
    "giá",
    "kết",
    "một",
    "những",
    "quả",
    "sự",
    "trị",
    "và",
}


@dataclass
class SourceVariant:
    surface: str
    match_type: str
    evidence_block_ids: list[str] = field(default_factory=list)
    occurrence_count: int = 0
    first_seen_window: str = ""

    def merge(
        self,
        *,
        evidence_block_ids: Iterable[str],
        occurrence_count: int,
    ) -> None:
        self.evidence_block_ids = _stable_unique([*self.evidence_block_ids, *evidence_block_ids])
        self.occurrence_count += int(occurrence_count or 0)


@dataclass
class TargetVariant:
    text: str
    evidence_block_id: str
    variant_reason: str


@dataclass
class ConflictRecord:
    type: str
    proposed_target: str | None
    reason: str
    window: str
    evidence_block_ids: list[str]


@dataclass
class DecisionLogEntry:
    action: str
    concept_key: str
    source_term: str
    window: str
    reason: str
    evidence_block_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.action not in DECISION_TYPES:
            raise ValueError(f"Unknown decision action: {self.action}")


@dataclass
class NotebookEntry:
    concept_key: str
    canonical_source_term: str
    canonical_target_vi: str
    term_type: str = "term"
    do_not_translate: bool = False
    status: str = NOTEBOOK_STATUS_OK
    occurrences_total: int = 0
    first_seen_window: str = ""
    source_variants: list[SourceVariant] = field(default_factory=list)
    target_variants: list[TargetVariant] = field(default_factory=list)
    conflict_ledger: list[ConflictRecord] = field(default_factory=list)
    decision_log: list[DecisionLogEntry] = field(default_factory=list)

    def source_surfaces(self) -> set[str]:
        return {variant.surface.casefold() for variant in self.source_variants}

    def target_surfaces(self) -> set[str]:
        values = {self.canonical_target_vi.casefold()} if self.canonical_target_vi else set()
        values.update(variant.text.casefold() for variant in self.target_variants)
        return values

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)


@dataclass
class Notebook:
    entries: dict[str, NotebookEntry] = field(default_factory=dict)
    rejected_terms: list[dict[str, Any]] = field(default_factory=list)
    decision_log: list[DecisionLogEntry] = field(default_factory=list)

    def log(
        self,
        action: str,
        *,
        key: str,
        source_term: str,
        window: str,
        reason: str,
        evidence_block_ids: Iterable[str] = (),
        entry: NotebookEntry | None = None,
    ) -> None:
        record = DecisionLogEntry(
            action=action,
            concept_key=key,
            source_term=source_term,
            window=window,
            reason=reason,
            evidence_block_ids=_stable_unique(evidence_block_ids),
        )
        self.decision_log.append(record)
        if entry is not None:
            entry.decision_log.append(record)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [
                self.entries[key].to_dict()
                for key in sorted(self.entries)
            ],
            "rejected_terms": sorted(
                self.rejected_terms,
                key=lambda item: (
                    str(item.get("concept_key") or ""),
                    str(item.get("source_term") or ""),
                    str(item.get("window") or ""),
                ),
            ),
            "decision_log": [_dataclass_to_dict(item) for item in self.decision_log],
        }


def apply_builder_output(
    notebook: Notebook,
    output: dict[str, Any],
    *,
    window_id: str,
    block_types_by_id: dict[str, str] | None = None,
) -> Notebook:
    block_types_by_id = block_types_by_id or {}
    for term in _list_of_dicts(output.get("new_terms")):
        apply_new_term(notebook, term, window_id=window_id, block_types_by_id=block_types_by_id)
    for update in _list_of_dicts(output.get("updates_to_existing")):
        apply_update(notebook, update, window_id=window_id)
    for conflict in _list_of_dicts(output.get("conflicts")):
        apply_conflict(notebook, conflict, window_id=window_id)
    for seen in _list_of_dicts(output.get("seen_existing_terms")):
        apply_seen_existing(notebook, seen, window_id=window_id)
    return notebook


def apply_new_term(
    notebook: Notebook,
    term: dict[str, Any],
    *,
    window_id: str,
    block_types_by_id: dict[str, str] | None = None,
) -> None:
    source_term = _clean(term.get("source_term"))
    if not source_term:
        return
    key = concept_key(source_term)
    evidence_block_ids = _evidence_ids(term)
    occurrence_count = _occurrence_count(term, evidence_block_ids)
    block_types = [str((block_types_by_id or {}).get(block_id) or "") for block_id in evidence_block_ids]

    if should_reject_stoplist(
        source_term,
        evidence_block_ids=evidence_block_ids,
        block_types=block_types,
        notebook=notebook,
    ):
        notebook.rejected_terms.append(
            {
                "source_term": source_term,
                "concept_key": key,
                "window": window_id,
                "evidence_block_ids": evidence_block_ids,
                "occurrence_count": occurrence_count,
                "reason": "single-token stoplist without strong termhood",
            }
        )
        notebook.log(
            "rejected_stoplist",
            key=key,
            source_term=source_term,
            window=window_id,
            reason="single-token stoplist without strong termhood",
            evidence_block_ids=evidence_block_ids,
        )
        return

    if key in notebook.entries:
        entry = notebook.entries[key]
        _merge_source_variant(
            notebook,
            entry,
            source_term=source_term,
            evidence_block_ids=evidence_block_ids,
            occurrence_count=occurrence_count,
            window_id=window_id,
            match_type="number_variant" if source_term.casefold() != entry.canonical_source_term.casefold() else "exact",
        )
        notebook.log(
            "merged_by_concept_key",
            key=key,
            source_term=source_term,
            window=window_id,
            reason="concept_key already exists",
            evidence_block_ids=evidence_block_ids,
            entry=entry,
        )
        _merge_target_from_term(notebook, entry, term, window_id=window_id, evidence_block_ids=evidence_block_ids)
        return

    entry = NotebookEntry(
        concept_key=key,
        canonical_source_term=source_term,
        canonical_target_vi=_clean(term.get("canonical_target_vi") or term.get("target_term")),
        term_type=_clean(term.get("term_type")) or "term",
        do_not_translate=bool(term.get("do_not_translate")),
        occurrences_total=occurrence_count,
        first_seen_window=window_id,
        source_variants=[
            SourceVariant(
                surface=source_term,
                match_type="exact",
                evidence_block_ids=evidence_block_ids,
                occurrence_count=occurrence_count,
                first_seen_window=window_id,
            )
        ],
    )
    notebook.entries[key] = entry
    notebook.log(
        "created",
        key=key,
        source_term=source_term,
        window=window_id,
        reason="new concept_key",
        evidence_block_ids=evidence_block_ids,
        entry=entry,
    )
    for variant in _target_variants_from(term):
        _add_target_variant(notebook, entry, variant, window_id=window_id, default_evidence=evidence_block_ids)


def apply_update(
    notebook: Notebook,
    update: dict[str, Any],
    *,
    window_id: str,
) -> None:
    source_term = _clean(update.get("source_term") or update.get("existing_source_term"))
    if not source_term:
        return
    key = concept_key(source_term)
    entry = notebook.entries.get(key)
    if entry is None:
        apply_new_term(notebook, update, window_id=window_id)
        return
    evidence_block_ids = _evidence_ids(update)
    occurrence_count = _occurrence_count(update, evidence_block_ids)
    source_variants = _source_variants_from(update) or [source_term]
    for surface in source_variants:
        _merge_source_variant(
            notebook,
            entry,
            source_term=surface,
            evidence_block_ids=evidence_block_ids,
            occurrence_count=occurrence_count if surface == source_variants[0] else 0,
            window_id=window_id,
            match_type="number_variant" if concept_key(surface) == key and surface.casefold() != entry.canonical_source_term.casefold() else "exact",
        )
    for variant in _target_variants_from(update):
        _add_target_variant(notebook, entry, variant, window_id=window_id, default_evidence=evidence_block_ids)


def apply_conflict(
    notebook: Notebook,
    conflict: dict[str, Any],
    *,
    window_id: str,
) -> None:
    source_term = _clean(conflict.get("source_term"))
    if not source_term:
        return
    key = concept_key(source_term)
    entry = notebook.entries.get(key)
    if entry is None:
        entry = NotebookEntry(
            concept_key=key,
            canonical_source_term=source_term,
            canonical_target_vi=_clean(conflict.get("existing_canonical_target_vi")),
            first_seen_window=window_id,
        )
        notebook.entries[key] = entry
    conflict_type = _clean(conflict.get("conflict_type") or conflict.get("type")) or "uncertain"
    evidence_block_ids = _evidence_ids(conflict)
    entry.conflict_ledger.append(
        ConflictRecord(
            type=conflict_type,
            proposed_target=_optional_clean(conflict.get("proposed_target_vi") or conflict.get("proposed_target")),
            reason=_clean(conflict.get("reason")) or "not specified",
            window=window_id,
            evidence_block_ids=evidence_block_ids,
        )
    )
    if conflict_type in CONFLICT_STATUS_TYPES:
        entry.status = NOTEBOOK_STATUS_CONFLICT
    notebook.log(
        "conflict_logged",
        key=key,
        source_term=source_term,
        window=window_id,
        reason=conflict_type,
        evidence_block_ids=evidence_block_ids,
        entry=entry,
    )


def apply_seen_existing(
    notebook: Notebook,
    seen: dict[str, Any],
    *,
    window_id: str,
) -> None:
    source_term = _clean(seen.get("source_term"))
    if not source_term:
        return
    key = concept_key(source_term)
    entry = notebook.entries.get(key)
    evidence_block_ids = _evidence_ids(seen)
    occurrence_count = _occurrence_count(seen, evidence_block_ids)
    if entry is None:
        apply_new_term(
            notebook,
            {
                "source_term": source_term,
                "canonical_target_vi": "",
                "evidence_block_ids": evidence_block_ids,
                "occurrence_count": occurrence_count,
            },
            window_id=window_id,
        )
        entry = notebook.entries.get(key)
    if entry is None:
        return
    _merge_source_variant(
        notebook,
        entry,
        source_term=source_term,
        evidence_block_ids=evidence_block_ids,
        occurrence_count=occurrence_count,
        window_id=window_id,
        match_type="exact",
        log_action=False,
    )
    notebook.log(
        "seen_existing",
        key=key,
        source_term=source_term,
        window=window_id,
        reason="visible existing term with no change",
        evidence_block_ids=evidence_block_ids,
        entry=entry,
    )


def should_reject_stoplist(
    source_term: str,
    *,
    evidence_block_ids: Iterable[str],
    block_types: Iterable[str],
    notebook: Notebook,
) -> bool:
    normalized = normalize_phrase(source_term)
    if not normalized or " " in normalized:
        return False
    if normalized not in STOPLIST_SINGLE_TOKENS:
        return False
    if normalized in ALLOWLIST_SINGLE_TOKENS:
        return False
    evidence = _stable_unique(evidence_block_ids)
    if len(evidence) >= 2:
        return False
    if any(str(block_type) in STRONG_TERM_BLOCK_TYPES for block_type in block_types):
        return False
    if concept_key(source_term) in notebook.entries:
        return False
    return True


def classify_target_divergence(existing_target: str, proposed_target: str) -> str:
    existing = _clean(existing_target)
    proposed = _clean(proposed_target)
    if not proposed or proposed.casefold() == existing.casefold():
        return "same"
    if _strip_vi_plural(existing).casefold() == _strip_vi_plural(proposed).casefold():
        return "plural_only_difference"
    if _looks_polysemous(existing, proposed):
        return "polysemy_suspected"
    return "synonym_or_style_variant"


def notebook_to_canonical_json(notebook: Notebook) -> str:
    return json.dumps(notebook.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_source_variant(
    notebook: Notebook,
    entry: NotebookEntry,
    *,
    source_term: str,
    evidence_block_ids: Iterable[str],
    occurrence_count: int,
    window_id: str,
    match_type: str,
    log_action: bool = True,
) -> None:
    clean = _clean(source_term)
    if not clean:
        return
    existing = next((item for item in entry.source_variants if item.surface.casefold() == clean.casefold()), None)
    if existing is None:
        entry.source_variants.append(
            SourceVariant(
                surface=clean,
                match_type=match_type,
                evidence_block_ids=_stable_unique(evidence_block_ids),
                occurrence_count=int(occurrence_count or 0),
                first_seen_window=window_id,
            )
        )
        if log_action:
            notebook.log(
                "updated_source_variant",
                key=entry.concept_key,
                source_term=clean,
                window=window_id,
                reason=f"added {match_type} source surface",
                evidence_block_ids=evidence_block_ids,
                entry=entry,
            )
    else:
        existing.merge(evidence_block_ids=evidence_block_ids, occurrence_count=int(occurrence_count or 0))
    entry.occurrences_total += int(occurrence_count or 0)


def _merge_target_from_term(
    notebook: Notebook,
    entry: NotebookEntry,
    term: dict[str, Any],
    *,
    window_id: str,
    evidence_block_ids: list[str],
) -> None:
    proposed = _clean(term.get("canonical_target_vi") or term.get("target_term"))
    divergence = classify_target_divergence(entry.canonical_target_vi, proposed)
    if divergence == "same":
        pass
    elif divergence == "plural_only_difference":
        entry.conflict_ledger.append(
            ConflictRecord(
                type="plural_only_difference",
                proposed_target=proposed,
                reason="Vietnamese target differs only by plural marker",
                window=window_id,
                evidence_block_ids=evidence_block_ids,
            )
        )
    elif divergence == "synonym_or_style_variant":
        _add_target_variant(
            notebook,
            entry,
            {"text": proposed, "evidence_block_id": _first(evidence_block_ids), "variant_reason": "same concept_key target variant"},
            window_id=window_id,
            default_evidence=evidence_block_ids,
        )
    else:
        entry.conflict_ledger.append(
            ConflictRecord(
                type=divergence,
                proposed_target=proposed,
                reason="canonical target divergence under same concept_key",
                window=window_id,
                evidence_block_ids=evidence_block_ids,
            )
        )
        entry.status = NOTEBOOK_STATUS_CONFLICT
        notebook.log(
            "conflict_logged",
            key=entry.concept_key,
            source_term=entry.canonical_source_term,
            window=window_id,
            reason=divergence,
            evidence_block_ids=evidence_block_ids,
            entry=entry,
        )
    for variant in _target_variants_from(term):
        _add_target_variant(notebook, entry, variant, window_id=window_id, default_evidence=evidence_block_ids)


def _add_target_variant(
    notebook: Notebook,
    entry: NotebookEntry,
    variant: dict[str, Any],
    *,
    window_id: str,
    default_evidence: list[str],
) -> None:
    text = _clean(variant.get("text") or variant.get("target_variant") or variant.get("target"))
    if not text:
        return
    divergence = classify_target_divergence(entry.canonical_target_vi, text)
    if divergence in {"same", "plural_only_difference"}:
        return
    if text.casefold() in entry.target_surfaces():
        return
    evidence_block_id = _clean(variant.get("evidence_block_id")) or _first(default_evidence)
    reason = _clean(variant.get("variant_reason") or variant.get("reason")) or "variant justified by source evidence"
    entry.target_variants.append(
        TargetVariant(text=text, evidence_block_id=evidence_block_id, variant_reason=reason)
    )
    notebook.log(
        "updated_target_variant",
        key=entry.concept_key,
        source_term=entry.canonical_source_term,
        window=window_id,
        reason=reason,
        evidence_block_ids=[evidence_block_id] if evidence_block_id else default_evidence,
        entry=entry,
    )


def _source_variants_from(update: dict[str, Any]) -> list[str]:
    variants = update.get("source_variants") or update.get("source_variant") or []
    if isinstance(variants, str):
        variants = [variants]
    values: list[str] = []
    for item in variants if isinstance(variants, list) else []:
        if isinstance(item, dict):
            values.append(_clean(item.get("surface") or item.get("source_term")))
        else:
            values.append(_clean(item))
    return [value for value in _stable_unique(values) if value]


def _target_variants_from(term: dict[str, Any]) -> list[dict[str, Any]]:
    variants = term.get("target_variants") or term.get("allowed_variants") or []
    if isinstance(variants, str):
        variants = [variants]
    results: list[dict[str, Any]] = []
    for item in variants if isinstance(variants, list) else []:
        if isinstance(item, dict):
            results.append(dict(item))
        else:
            results.append({"text": _clean(item), "variant_reason": "existing allowed variant"})
    return results


def _evidence_ids(term: dict[str, Any]) -> list[str]:
    return [
        _clean(item)
        for item in term.get("evidence_block_ids")
        or term.get("evidence_span_ids")
        or term.get("block_ids")
        or []
        if _clean(item)
    ]


def _occurrence_count(term: dict[str, Any], evidence_block_ids: list[str]) -> int:
    raw = term.get("occurrence_count", term.get("occurrences_count"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else max(1, len(evidence_block_ids))


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _strip_vi_plural(value: str) -> str:
    result = _clean(value)
    lowered = result.casefold()
    for marker in VI_PLURAL_MARKERS:
        if lowered.startswith(marker):
            return result[len(marker):].strip()
    return result


def _looks_polysemous(existing: str, proposed: str) -> bool:
    left = _meaningful_vi_tokens(existing)
    right = _meaningful_vi_tokens(proposed)
    if not left or not right:
        return False
    if left.isdisjoint(right):
        return True
    existing_l = normalize_phrase(existing)
    proposed_l = normalize_phrase(proposed)
    if ("hàm" in existing_l) != ("hàm" in proposed_l) and ("giá trị" in existing_l or "giá trị" in proposed_l):
        return True
    return False


def _meaningful_vi_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"\w+", normalize_phrase(value), flags=re.UNICODE)
        if token not in VI_LIGHT_TOKENS
    }


def _stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        clean = _clean(value)
        marker = clean.casefold()
        if not clean or marker in seen:
            continue
        seen.add(marker)
        results.append(clean)
    return sorted(results, key=lambda item: (item.casefold(), item))


def _dataclass_to_dict(value: Any) -> dict[str, Any]:
    raw = asdict(value)
    return _sort_nested(raw)


def _sort_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sort_nested(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_nested(item) for item in value]
    return value


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _optional_clean(value: Any) -> str | None:
    clean = _clean(value)
    return clean or None


def _first(values: list[str]) -> str:
    return values[0] if values else ""
