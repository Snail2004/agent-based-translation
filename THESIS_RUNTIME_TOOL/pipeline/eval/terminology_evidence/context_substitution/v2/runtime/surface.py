from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping


def normalized_surface(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def surfaces_equivalent(left: str, right: str) -> bool:
    return normalized_surface(left) == normalized_surface(right)


def contains_surface(text: str, surface: str) -> bool:
    normalized_text = normalized_surface(text)
    normalized_candidate = normalized_surface(surface)
    if not normalized_candidate:
        return False
    pattern = re.escape(normalized_candidate).replace(r"\ ", r"\s+")
    return (
        re.search(
            rf"(?<![\w]){pattern}(?![\w])",
            normalized_text,
            flags=re.UNICODE,
        )
        is not None
    )


def trial_surface_binding(
    *,
    canonical_target: str,
    trial: Mapping[str, Any],
) -> tuple[bool, str | None]:
    surface_used = str(trial.get("candidate_surface_used") or "")
    expansion_raw = trial.get("applied_expansion")
    expansion = str(expansion_raw) if isinstance(expansion_raw, str) else None
    trial_text = str(trial.get("trial_translation") or "")
    usage_confirmed = trial.get("candidate_usage_confirmed") is True
    if not usage_confirmed:
        return False, None
    if surfaces_equivalent(surface_used, canonical_target):
        observed_surface = canonical_target
    elif (
        expansion is not None
        and surfaces_equivalent(surface_used, expansion)
        and contains_surface(expansion, canonical_target)
    ):
        observed_surface = expansion
    else:
        return False, None
    if not contains_surface(trial_text, canonical_target):
        return False, None
    if not contains_surface(trial_text, observed_surface):
        return False, None
    return True, observed_surface


