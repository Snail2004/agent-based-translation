"""Deterministic, offline linting for assembled B4 translations."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re
from typing import Any, Mapping, Sequence

from pipeline.literary.b4_translator_pack_v1 import (
    B4TranslatorPackError,
    verify_translator_pack_v1,
)
from pipeline.literary.checkpoint import canonical_hash


LINT_REPORT_SCHEMA_VERSION = "literary_b4_translation_lint_report_v2"
LINT_POLICY_SCHEMA_VERSION = "literary_b4_translation_lint_policy_v1"
CORRECTED_TRANSLATION_SCHEMA_VERSION = (
    "literary_b4_translation_mechanically_corrected_v1"
)

_HONORIFIC_RE = re.compile(r"(?<!\w)(?:Mr|Mrs|Miss|Ms)\.(?=\s|$)")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_SOURCE_CARRY_THROUGH_TOKEN_RE = re.compile(
    r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*",
    re.UNICODE,
)
_MIN_SOURCE_CARRY_THROUGH_TOKEN_LENGTH = 4
_MOJIBAKE_MARKERS = (
    "\ufffd",
    "\u00c3\u00a0",
    "\u00c3\u00a1",
    "\u00c3\u00a2",
    "\u00c3\u00a3",
    "\u00c3\u00a8",
    "\u00c3\u00a9",
    "\u00c3\u00aa",
    "\u00c3\u00ac",
    "\u00c3\u00ad",
    "\u00c3\u00b2",
    "\u00c3\u00b3",
    "\u00c3\u00b4",
    "\u00c3\u00b9",
    "\u00c3\u00ba",
    "\u00c4\u2018",
    "\u00c6\u00a1",
    "\u00c6\u00b0",
    "\u00e2\u20ac\u0153",
    "\u00e2\u20ac\u009d",
    "\u00e2\u20ac\u2122",
    "\u00ef\u00bf\u00bd",
)


class B4TranslationLintError(RuntimeError):
    pass


def lint_translation_chapter_v1(
    *,
    translation_artifact: Mapping[str, Any],
    chapter: Mapping[str, Any],
    window_plan: Mapping[str, Any],
    translator_pack: Mapping[str, Any] | None = None,
    mechanical_policy: Mapping[str, Any] | None = None,
    apply_mechanical_fixes: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    translation = _verify_translation_artifact(translation_artifact)
    verified_pack = _verified_translator_pack(
        translator_pack=translator_pack,
        translation=translation,
    )
    policy = _validated_policy(mechanical_policy)
    chapter_id = _text(chapter.get("chapter_id"), "chapter_id")
    if translation.get("chapter_id") != chapter_id:
        raise B4TranslationLintError("translation and source chapter differ")
    if window_plan.get("chapter_id") not in (None, chapter_id):
        raise B4TranslationLintError("window plan and source chapter differ")
    if (
        translation.get("window_plan_hash") is not None
        and window_plan.get("window_plan_hash")
        != translation.get("window_plan_hash")
    ):
        raise B4TranslationLintError("translation window plan hash differs")

    source_rows = _source_rows(chapter)
    source_by_id = {row["block_id"]: row for row in source_rows}
    expected_ids = _expected_active_ids(window_plan)
    translated_rows = translation.get("blocks")
    if not isinstance(translated_rows, list):
        raise B4TranslationLintError("translation blocks are malformed")
    translated_ids = [
        _text(row.get("block_id"), "translated block_id")
        for row in translated_rows
        if isinstance(row, Mapping)
    ]
    if len(translated_ids) != len(translated_rows):
        raise B4TranslationLintError("translated block is malformed")
    if translated_ids != expected_ids:
        raise B4TranslationLintError(
            "translation does not exact-cover window plan in order"
        )
    for row in translated_rows:
        block_id = str(row["block_id"])
        if block_id not in source_by_id:
            raise B4TranslationLintError(
                "translation cites a block absent from the source chapter"
            )
        if row.get("source_text") != source_by_id[block_id]["source_text"]:
            raise B4TranslationLintError(
                "translation source_text is not verbatim"
            )
        _text(row.get("target_text"), "target_text")

    source_issues = _collect_issues(
        translated_rows=translated_rows,
        policy=policy,
    )
    observations = (
        _collect_source_carry_through_observations(
            translated_rows=translated_rows,
            translator_pack=verified_pack,
        )
        if verified_pack is not None
        else []
    )
    corrected_artifact = None
    corrections: list[dict[str, Any]] = []
    remaining_issues = source_issues
    if apply_mechanical_fixes:
        corrected_rows, corrections = _apply_replacements(
            translated_rows=translated_rows,
            replacements=policy["replacements"],
        )
        corrected_body = {
            **{
                key: deepcopy(value)
                for key, value in translation.items()
                if key not in {"schema_version", "artifact_hash"}
            },
            "schema_version": CORRECTED_TRANSLATION_SCHEMA_VERSION,
            "source_translation_schema_version": translation["schema_version"],
            "source_translation_artifact_hash": translation["artifact_hash"],
            "lint_policy_hash": policy["policy_hash"],
            "blocks": corrected_rows,
            "mechanical_corrections": corrections,
            "translation_text_mutation_performed": bool(corrections),
            "semantic_record_mutation_performed": False,
            "lint_provider_calls": 0,
        }
        corrected_artifact = {
            **corrected_body,
            "artifact_hash": canonical_hash(corrected_body),
        }
        remaining_issues = _collect_issues(
            translated_rows=corrected_rows,
            policy=policy,
        )

    issue_counts = Counter(row["issue_kind"] for row in source_issues)
    remaining_counts = Counter(row["issue_kind"] for row in remaining_issues)
    observation_counts = Counter(
        row["observation_kind"] for row in observations
    )
    report_body = {
        "schema_version": LINT_REPORT_SCHEMA_VERSION,
        "status": "clean" if not source_issues else "issues_found",
        "book_id": translation.get("book_id"),
        "chapter_id": chapter_id,
        "source_translation_artifact_hash": translation["artifact_hash"],
        "source_translation_schema_version": translation["schema_version"],
        "window_plan_hash": window_plan.get("window_plan_hash"),
        "lint_policy_hash": policy["policy_hash"],
        "translated_block_count": len(translated_rows),
        "issue_count": len(source_issues),
        "issue_by_kind": dict(sorted(issue_counts.items())),
        "issues": source_issues,
        "source_carry_through_checked": verified_pack is not None,
        "translator_pack_artifact_hash": (
            verified_pack["artifact_hash"]
            if verified_pack is not None
            else None
        ),
        "observation_count": len(observations),
        "observation_by_kind": dict(sorted(observation_counts.items())),
        "observations": observations,
        "mechanical_fix_requested": apply_mechanical_fixes,
        "mechanical_correction_count": sum(
            int(row["replacement_count"]) for row in corrections
        ),
        "mechanical_corrections": corrections,
        "remaining_issue_count": len(remaining_issues),
        "remaining_issue_by_kind": dict(sorted(remaining_counts.items())),
        "remaining_issues": remaining_issues,
        "corrected_translation_artifact_hash": (
            corrected_artifact["artifact_hash"]
            if corrected_artifact is not None
            else None
        ),
        "provider_calls": 0,
        "semantic_record_mutation_performed": False,
    }
    report = {
        **report_body,
        "artifact_hash": canonical_hash(report_body),
    }
    return report, corrected_artifact


def _verified_translator_pack(
    *,
    translator_pack: Mapping[str, Any] | None,
    translation: Mapping[str, Any],
) -> dict[str, Any] | None:
    if translator_pack is None:
        return None
    try:
        pack = verify_translator_pack_v1(translator_pack)
    except B4TranslatorPackError as exc:
        raise B4TranslationLintError(
            "Translator Pack is invalid for translation lint"
        ) from exc
    if pack.get("chapter_id") != translation.get("chapter_id"):
        raise B4TranslationLintError(
            "Translator Pack and translation chapter differ"
        )
    if (
        translation.get("translator_pack_artifact_hash")
        != pack.get("artifact_hash")
    ):
        raise B4TranslationLintError(
            "Translator Pack and translation lineage differ"
        )
    return pack


def _collect_source_carry_through_observations(
    *,
    translated_rows: Sequence[Mapping[str, Any]],
    translator_pack: Mapping[str, Any],
) -> list[dict[str, Any]]:
    authorized_tokens = _pack_entity_surface_tokens(translator_pack)
    observations: list[dict[str, Any]] = []
    for row in translated_rows:
        block_id = str(row["block_id"])
        source_counts = Counter(
            _SOURCE_CARRY_THROUGH_TOKEN_RE.findall(str(row["source_text"]))
        )
        target_counts = Counter(
            _SOURCE_CARRY_THROUGH_TOKEN_RE.findall(str(row["target_text"]))
        )
        for token in sorted(source_counts.keys() & target_counts.keys()):
            if len(token) < _MIN_SOURCE_CARRY_THROUGH_TOKEN_LENGTH:
                continue
            if token.casefold() in authorized_tokens:
                continue
            observation_body = {
                "observation_kind": "verbatim_source_token_carry_through",
                "block_id": block_id,
                "token": token,
                "occurrence_count": min(
                    source_counts[token],
                    target_counts[token],
                ),
            }
            observations.append(
                {
                    "observation_id": (
                        f"b4obs1_{canonical_hash(observation_body)[:20]}"
                    ),
                    **observation_body,
                }
            )
    observations.sort(
        key=lambda row: (
            row["block_id"],
            row["observation_kind"],
            row["token"],
        )
    )
    return observations


def _pack_entity_surface_tokens(
    translator_pack: Mapping[str, Any],
) -> set[str]:
    tokens: set[str] = set()
    for entity in translator_pack.get("entities") or []:
        if not isinstance(entity, Mapping):
            continue
        for field in ("canonical_surface", "stable_surfaces", "aliases"):
            raw = entity.get(field)
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                if not isinstance(value, str):
                    continue
                tokens.update(
                    token.casefold()
                    for token in _SOURCE_CARRY_THROUGH_TOKEN_RE.findall(
                        value
                    )
                )
    return tokens


def _collect_issues(
    *,
    translated_rows: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row in translated_rows:
        block_id = str(row["block_id"])
        source_text = str(row["source_text"])
        target_text = str(row["target_text"])
        for match in _HONORIFIC_RE.finditer(target_text):
            issues.append(
                _issue(
                    issue_kind="untranslated_english_honorific",
                    severity="warning",
                    block_id=block_id,
                    evidence=match.group(0),
                    occurrence_count=1,
                )
            )
        cyrillic = _CYRILLIC_RE.findall(target_text)
        if cyrillic:
            issues.append(
                _issue(
                    issue_kind="unexpected_cyrillic",
                    severity="warning",
                    block_id=block_id,
                    evidence="".join(cyrillic[:20]),
                    occurrence_count=len(cyrillic),
                )
            )
        for marker in _MOJIBAKE_MARKERS:
            count = target_text.count(marker)
            if count:
                issues.append(
                    _issue(
                        issue_kind="possible_mojibake",
                        severity="warning",
                        block_id=block_id,
                        evidence=marker,
                        occurrence_count=count,
                    )
                )
        if target_text.count("\u201c") != target_text.count("\u201d"):
            issues.append(
                _issue(
                    issue_kind="unbalanced_curly_double_quotes",
                    severity="warning",
                    block_id=block_id,
                    evidence=(
                        f"open={target_text.count(chr(0x201c))};"
                        f"close={target_text.count(chr(0x201d))}"
                    ),
                    occurrence_count=1,
                )
            )
        if target_text.count('"') % 2:
            issues.append(
                _issue(
                    issue_kind="unbalanced_ascii_double_quotes",
                    severity="warning",
                    block_id=block_id,
                    evidence=f'count={target_text.count(chr(34))}',
                    occurrence_count=1,
                )
            )
        for watch in policy["watch_literals"]:
            literal = watch["literal"]
            count = target_text.count(literal)
            if count:
                issues.append(
                    _issue(
                        issue_kind=watch["issue_kind"],
                        severity=watch["severity"],
                        block_id=block_id,
                        evidence=literal,
                        occurrence_count=count,
                    )
                )
        for replacement in policy["replacements"]:
            literal = replacement["from"]
            count = target_text.count(literal)
            if count:
                issues.append(
                    _issue(
                        issue_kind=replacement["issue_kind"],
                        severity="warning",
                        block_id=block_id,
                        evidence=literal,
                        occurrence_count=count,
                        suggested_replacement=replacement["to"],
                    )
                )
        for rule in policy["glossary_rules"]:
            block_scope = set(rule["block_ids"])
            if block_scope and block_id not in block_scope:
                continue
            if not any(term in source_text for term in rule["source_terms"]):
                continue
            if not any(term in target_text for term in rule["required_targets"]):
                issues.append(
                    _issue(
                        issue_kind="glossary_target_missing",
                        severity="warning",
                        block_id=block_id,
                        evidence=" | ".join(rule["source_terms"]),
                        occurrence_count=1,
                        suggested_replacement=" | ".join(
                            rule["required_targets"]
                        ),
                    )
                )
            for forbidden in rule["forbidden_targets"]:
                count = target_text.count(forbidden)
                if count:
                    issues.append(
                        _issue(
                            issue_kind="glossary_forbidden_target",
                            severity="warning",
                            block_id=block_id,
                            evidence=forbidden,
                            occurrence_count=count,
                        )
                    )
    issues.sort(
        key=lambda row: (
            row["block_id"],
            row["issue_kind"],
            row["evidence"],
        )
    )
    return issues


def _apply_replacements(
    *,
    translated_rows: Sequence[Mapping[str, Any]],
    replacements: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    corrected = [deepcopy(dict(row)) for row in translated_rows]
    if not replacements:
        return corrected, []
    by_source = {row["from"]: row for row in replacements}
    pattern = re.compile(
        "|".join(
            re.escape(source)
            for source in sorted(by_source, key=lambda value: (-len(value), value))
        )
    )
    corrections: list[dict[str, Any]] = []
    for row in corrected:
        original = str(row["target_text"])
        counts: Counter[str] = Counter()

        def replace(match: re.Match[str]) -> str:
            source = match.group(0)
            counts[source] += 1
            return by_source[source]["to"]

        row["target_text"] = pattern.sub(replace, original)
        for source, count in sorted(counts.items()):
            corrections.append(
                {
                    "block_id": str(row["block_id"]),
                    "from": source,
                    "to": by_source[source]["to"],
                    "replacement_count": count,
                    "rule_id": by_source[source]["rule_id"],
                }
            )
    return corrected, corrections


def _validated_policy(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = deepcopy(dict(value)) if value is not None else {
        "schema_version": LINT_POLICY_SCHEMA_VERSION,
        "replacements": [],
        "watch_literals": [],
        "glossary_rules": [],
    }
    if raw.get("schema_version") != LINT_POLICY_SCHEMA_VERSION:
        raise B4TranslationLintError("unsupported translation lint policy")
    replacements = raw.get("replacements")
    watch_literals = raw.get("watch_literals")
    glossary_rules = raw.get("glossary_rules")
    if not isinstance(replacements, list):
        raise B4TranslationLintError("lint replacements must be a list")
    if not isinstance(watch_literals, list):
        raise B4TranslationLintError("lint watch_literals must be a list")
    if not isinstance(glossary_rules, list):
        raise B4TranslationLintError("lint glossary_rules must be a list")

    normalized_replacements = []
    seen_sources: set[str] = set()
    for index, row in enumerate(replacements):
        if not isinstance(row, Mapping):
            raise B4TranslationLintError("lint replacement is malformed")
        source = _text(row.get("from"), "replacement from")
        target = _text(row.get("to"), "replacement to")
        if source == target or source in seen_sources:
            raise B4TranslationLintError(
                "lint replacement is empty, repeated, or has no effect"
            )
        seen_sources.add(source)
        normalized_replacements.append(
            {
                "rule_id": _text(
                    row.get("rule_id") or f"replacement_{index + 1}",
                    "replacement rule_id",
                ),
                "from": source,
                "to": target,
                "issue_kind": _text(
                    row.get("issue_kind") or "configured_noncanonical_form",
                    "replacement issue_kind",
                ),
            }
        )

    normalized_watch = []
    for row in watch_literals:
        if not isinstance(row, Mapping):
            raise B4TranslationLintError("lint watch literal is malformed")
        severity = str(row.get("severity") or "warning")
        if severity not in {"warning", "error"}:
            raise B4TranslationLintError("lint severity is unsupported")
        normalized_watch.append(
            {
                "literal": _text(row.get("literal"), "watch literal"),
                "issue_kind": _text(
                    row.get("issue_kind"), "watch issue_kind"
                ),
                "severity": severity,
            }
        )

    normalized_glossary = []
    for row in glossary_rules:
        if not isinstance(row, Mapping):
            raise B4TranslationLintError("lint glossary rule is malformed")
        source_terms = _string_list(
            row.get("source_terms"), "glossary source_terms", allow_empty=False
        )
        required_targets = _string_list(
            row.get("required_targets"),
            "glossary required_targets",
            allow_empty=False,
        )
        normalized_glossary.append(
            {
                "rule_id": _text(row.get("rule_id"), "glossary rule_id"),
                "source_terms": source_terms,
                "required_targets": required_targets,
                "forbidden_targets": _string_list(
                    row.get("forbidden_targets") or [],
                    "glossary forbidden_targets",
                    allow_empty=True,
                ),
                "block_ids": _string_list(
                    row.get("block_ids") or [],
                    "glossary block_ids",
                    allow_empty=True,
                ),
            }
        )
    body = {
        "schema_version": LINT_POLICY_SCHEMA_VERSION,
        "replacements": normalized_replacements,
        "watch_literals": normalized_watch,
        "glossary_rules": normalized_glossary,
    }
    return {**body, "policy_hash": canonical_hash(body)}


def _verify_translation_artifact(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(value))
    artifact_hash = raw.pop("artifact_hash", None)
    if not isinstance(artifact_hash, str) or artifact_hash != canonical_hash(raw):
        raise B4TranslationLintError("translation artifact hash differs")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.startswith(
        "literary_b4_translation_chapter_v"
    ):
        raise B4TranslationLintError("unsupported translation artifact schema")
    return {**raw, "artifact_hash": artifact_hash}


def _source_rows(chapter: Mapping[str, Any]) -> list[dict[str, str]]:
    rows = chapter.get("blocks")
    if not isinstance(rows, list) or not rows:
        raise B4TranslationLintError("source chapter has no blocks")
    result = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise B4TranslationLintError("source block is malformed")
        block_id = _text(row.get("block_id"), "source block_id")
        source_text = _text(row.get("clean_text"), "source clean_text")
        if block_id in seen:
            raise B4TranslationLintError("source chapter repeats a block")
        seen.add(block_id)
        result.append({"block_id": block_id, "source_text": source_text})
    return result


def _expected_active_ids(window_plan: Mapping[str, Any]) -> list[str]:
    windows = window_plan.get("windows")
    if not isinstance(windows, list) or not windows:
        raise B4TranslationLintError("window plan has no windows")
    result = []
    for window in windows:
        if not isinstance(window, Mapping):
            raise B4TranslationLintError("window plan row is malformed")
        result.extend(
            _string_list(
                window.get("active_block_ids"),
                "active_block_ids",
                allow_empty=False,
            )
        )
    if len(result) != len(set(result)):
        raise B4TranslationLintError("window plan repeats an active block")
    return result


def _issue(
    *,
    issue_kind: str,
    severity: str,
    block_id: str,
    evidence: str,
    occurrence_count: int,
    suggested_replacement: str | None = None,
) -> dict[str, Any]:
    identity = {
        "issue_kind": issue_kind,
        "block_id": block_id,
        "evidence": evidence,
        "suggested_replacement": suggested_replacement,
    }
    return {
        "issue_id": f"b4lint1_{canonical_hash(identity)[:20]}",
        "issue_kind": issue_kind,
        "severity": severity,
        "block_id": block_id,
        "evidence": evidence,
        "occurrence_count": occurrence_count,
        "suggested_replacement": suggested_replacement,
    }


def _string_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool,
) -> list[str]:
    if not isinstance(value, list):
        raise B4TranslationLintError(f"{label} must be a list")
    result = [_text(row, label) for row in value]
    if not allow_empty and not result:
        raise B4TranslationLintError(f"{label} must not be empty")
    if len(result) != len(set(result)):
        raise B4TranslationLintError(f"{label} repeats a value")
    return result


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B4TranslationLintError(f"{label} must be non-empty text")
    return value


__all__ = [
    "B4TranslationLintError",
    "CORRECTED_TRANSLATION_SCHEMA_VERSION",
    "LINT_POLICY_SCHEMA_VERSION",
    "LINT_REPORT_SCHEMA_VERSION",
    "lint_translation_chapter_v1",
]
