"""D2L-specific soft glossary rendering and override validation."""

from __future__ import annotations

import unicodedata
from typing import Any, Mapping, Sequence


POLICY_ID = "d2l_soft_glossary_policy_v1_3"
OVERRIDE_MATCH_RULE_ID = "unicode_nfkc_casefold_alnum_tokens_exact_once_v1"
TERM_OVERRIDES_KEY = "__term_overrides__"
OVERRIDE_REASON_CODES = frozenset(
    {
        "different_source_sense",
        "grammar_or_fluency",
        "technical_register",
        "source_does_not_express_term",
        "other",
    }
)
_OVERRIDE_FIELDS = frozenset(
    {
        "source_term",
        "preferred_target_vi",
        "used_target_vi",
        "block_id",
        "reason_code",
    }
)


def render_soft_glossary_context(
    context_pack: Any | None,
    *,
    include_override_log: bool = True,
) -> str:
    preferred = _string_lines(context_pack, "glossary_lines")
    contextual = _string_lines(context_pack, "context_sensitive_lines")
    preserve = _string_lines(context_pack, "preserve_lines")
    sections = [
        "PREFERRED TECHNICAL TERMS",
        "Use the target for the same technical sense; otherwise translate naturally.",
        *([f"- {line}" for line in preferred] or ["(none)"]),
    ]
    if contextual:
        sections.extend(
            [
                "CONTEXT-SENSITIVE TERMS",
                (
                    "Treat the arrow target and all listed alternatives as an "
                    "UNORDERED set of allowed choices. Their position and the storage "
                    "label 'alternatives' carry no semantic preference. Choose only by "
                    "the local source sense: when a WHEN condition fits, use that target; "
                    "never default to the arrow target merely because it appears first."
                ),
                *[f"- {line}" for line in contextual],
            ]
        )
    if preserve:
        sections.extend(
            [
                "PRESERVE EXACTLY",
                "Keep these source tokens unchanged.",
                *[f"- {line}" for line in preserve],
            ]
        )
    if include_override_log and (preferred or contextual):
        sections.extend(
            [
                "OPTIONAL OVERRIDE LOG",
                (
                    f"If you deliberately use another Vietnamese target for an injected "
                    f"soft term, add top-level {TERM_OVERRIDES_KEY}; omit it or use [] "
                    "when none. A listed contextual alternative is still an override and "
                    "must be logged. For contextual rules, preferred_target_vi is stored "
                    "lineage metadata, not a semantic preference. Never override preserve "
                    "rules."
                ),
                (
                    "Each row has exactly source_term, preferred_target_vi, used_target_vi, "
                    "block_id, reason_code. Copy the first two from the rule. reason_code: "
                    + ", ".join(sorted(OVERRIDE_REASON_CODES))
                    + ". used_target_vi must be one contiguous phrase appearing exactly "
                    "once in that block's translation."
                ),
            ]
        )
    return "\n".join(sections)


def injected_override_sources(context_pack: Any | None) -> set[str]:
    return set(injected_override_preferences(context_pack))


def injected_override_preferences(
    context_pack: Any | None,
) -> dict[str, set[str]]:
    preferences: dict[str, set[str]] = {}
    for field in ("glossary_lines", "context_sensitive_lines"):
        for line in _string_lines(context_pack, field):
            source, separator, target = line.partition(" -> ")
            if not separator or not source.strip() or not target.strip():
                continue
            if field == "context_sensitive_lines":
                target = target.split(" (context-sensitive", 1)[0]
            normalized_source = _normalize(source)
            preferences.setdefault(normalized_source, set()).add(_normalize(target))
    return preferences


def injected_target_options(
    context_pack: Any | None,
) -> dict[str, set[str]]:
    """Return all mechanically rendered targets, including contextual alternatives."""

    options = injected_override_preferences(context_pack)
    for line in _string_lines(context_pack, "context_sensitive_lines"):
        source, separator, target = line.partition(" -> ")
        if not separator or not source.strip() or not target.strip():
            continue
        normalized_source = _normalize(source)
        marker = "; alternatives: "
        if marker not in target:
            continue
        alternatives = target.split(marker, 1)[1].rsplit("; do not force)", 1)[0]
        for rendered_rule in alternatives.split("; "):
            alternative = rendered_rule.split(" when: ", 1)[0].strip()
            if alternative:
                options.setdefault(normalized_source, set()).add(
                    _normalize(alternative)
                )
    return options


def split_term_override_metadata(
    parsed_json: Mapping[str, Any] | None,
    *,
    expected_block_ids: Sequence[str],
    allowed_source_terms: set[str],
    allowed_preferred_targets: Mapping[str, set[str]] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]], bool, list[str]]:
    """Remove and validate optional override metadata from a translation payload."""

    if parsed_json is None:
        return None, [], False, []
    payload = dict(parsed_json)
    if TERM_OVERRIDES_KEY not in payload:
        return payload, [], False, []
    raw_rows = payload.pop(TERM_OVERRIDES_KEY)
    if not isinstance(raw_rows, list):
        return payload, [], True, [f"{TERM_OVERRIDES_KEY} must be an array"]

    expected = set(expected_block_ids)
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_rows):
        label = f"{TERM_OVERRIDES_KEY}[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != _OVERRIDE_FIELDS:
            errors.append(f"{label} has invalid fields")
            continue
        row: dict[str, str] = {}
        invalid = False
        for field in sorted(_OVERRIDE_FIELDS):
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}.{field} must be nonempty text")
                invalid = True
            else:
                row[field] = value.strip()
        if invalid:
            continue
        if row["block_id"] not in expected:
            errors.append(f"{label}.block_id is outside the source window")
        normalized_source = _normalize(row["source_term"])
        if normalized_source not in allowed_source_terms:
            errors.append(f"{label}.source_term was not injected as a soft term")
        elif allowed_preferred_targets is not None:
            valid_targets = allowed_preferred_targets.get(normalized_source, set())
            if _normalize(row["preferred_target_vi"]) not in valid_targets:
                errors.append(
                    f"{label}.preferred_target_vi does not match the injected preference"
                )
        if row["reason_code"] not in OVERRIDE_REASON_CODES:
            errors.append(f"{label}.reason_code is invalid")
        if _normalize(row["preferred_target_vi"]) == _normalize(row["used_target_vi"]):
            errors.append(f"{label} does not describe an actual override")
        key = (row["block_id"], _normalize(row["source_term"]))
        if key in seen:
            errors.append(f"{label} duplicates an override in the same block")
        seen.add(key)
        translated = payload.get(row["block_id"])
        if row["block_id"] in expected and isinstance(translated, str):
            occurrence_count = _token_sequence_count(
                translated,
                row["used_target_vi"],
            )
            if occurrence_count == 0:
                errors.append(
                    f"{label}.used_target_vi is absent from the translated block "
                    f"under {OVERRIDE_MATCH_RULE_ID}"
                )
            elif occurrence_count > 1:
                errors.append(
                    f"{label}.used_target_vi is ambiguous in the translated block "
                    f"under {OVERRIDE_MATCH_RULE_ID}: {occurrence_count} matches"
                )
        elif row["block_id"] in expected:
            errors.append(
                f"{label}.used_target_vi cannot be verified against a text translation"
            )
        rows.append(row)
    return payload, rows, True, errors


def _string_lines(context_pack: Any | None, field: str) -> list[str]:
    if context_pack is None:
        return []
    return [
        str(value).strip()
        for value in (getattr(context_pack, field, None) or [])
        if str(value).strip()
    ]


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _match_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    token_text = "".join(character if character.isalnum() else " " for character in normalized)
    return tuple(token_text.split())


def _token_sequence_count(text: str, phrase: str) -> int:
    text_tokens = _match_tokens(text)
    phrase_tokens = _match_tokens(phrase)
    if not phrase_tokens or len(phrase_tokens) > len(text_tokens):
        return 0
    width = len(phrase_tokens)
    return sum(
        1
        for index in range(len(text_tokens) - width + 1)
        if text_tokens[index : index + width] == phrase_tokens
    )


def token_sequence_count(text: str, phrase: str) -> int:
    """Count normalized alphanumeric token-sequence matches deterministically."""

    return _token_sequence_count(text, phrase)


__all__ = [
    "OVERRIDE_REASON_CODES",
    "OVERRIDE_MATCH_RULE_ID",
    "POLICY_ID",
    "TERM_OVERRIDES_KEY",
    "injected_override_preferences",
    "injected_override_sources",
    "injected_target_options",
    "render_soft_glossary_context",
    "split_term_override_metadata",
    "token_sequence_count",
]
