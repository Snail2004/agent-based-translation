from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


FLAGGED_SCRIPTS = {"Cyrillic", "CJK", "Hangul", "Thai"}


@dataclass(frozen=True)
class HygieneIssue:
    block_id: str
    script: str
    surface: str
    start: int
    end: int
    evidence_source: str
    evidence_target: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "script": self.script,
            "surface": self.surface,
            "start": self.start,
            "end": self.end,
            "evidence_source": self.evidence_source,
            "evidence_target": self.evidence_target,
        }


def detect_hygiene_issues(
    blocks_for_prompt: list[dict[str, Any]],
    translations: dict[str, str],
) -> list[HygieneIssue]:
    """Find non-source scripts in output translations.

    This is a mechanical script-diff check: scripts already present in the
    source block are allowed, while suspicious output-only scripts are flagged.
    Latin is intentionally ignored because Vietnamese and proper names use it.
    """

    source_by_block = {
        str(block.get("block_id")): str(
            block.get("source_text") or block.get("clean_text") or ""
        )
        for block in blocks_for_prompt
    }
    issues: list[HygieneIssue] = []
    for block_id, target_text in translations.items():
        source_text = source_by_block.get(block_id, "")
        allowed_scripts = _scripts_in(source_text)
        for span in _script_spans(str(target_text)):
            if span["script"] not in FLAGGED_SCRIPTS:
                continue
            if span["script"] in allowed_scripts:
                continue
            issues.append(
                HygieneIssue(
                    block_id=block_id,
                    script=span["script"],
                    surface=span["surface"],
                    start=int(span["start"]),
                    end=int(span["end"]),
                    evidence_source=_snippet(source_text, 0, min(len(source_text), 160)),
                    evidence_target=_snippet(
                        str(target_text), int(span["start"]), int(span["end"])
                    ),
                )
            )
    return issues


def hygiene_reask_note(issues: list[HygieneIssue]) -> str:
    samples = "; ".join(
        f"{issue.block_id}:{issue.script}:{issue.surface}" for issue in issues[:5]
    )
    extra = "" if len(issues) <= 5 else f"; +{len(issues) - 5} more"
    return (
        "Your previous JSON was structurally valid, but some translations contained "
        "non-Vietnamese-script characters that do not appear in the source block "
        f"({samples}{extra}). Retranslate the same window into Vietnamese. Keep any "
        "source formulas, code tokens, names, or symbols that are present in the source. "
        "Return a valid JSON object with exactly the same block_ids as keys and full "
        "Vietnamese translation strings as values. No extra keys."
    )


def _scripts_in(text: str) -> set[str]:
    return {script for script in (_script_of(ch) for ch in text) if script}


def _script_spans(text: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    active_script: str | None = None
    active_start: int | None = None
    for index, char in enumerate(text):
        script = _script_of(char)
        if script != active_script:
            if active_script and active_start is not None:
                spans.append(
                    {
                        "script": active_script,
                        "surface": text[active_start:index],
                        "start": active_start,
                        "end": index,
                    }
                )
            active_script = script
            active_start = index if script else None
    if active_script and active_start is not None:
        spans.append(
            {
                "script": active_script,
                "surface": text[active_start:],
                "start": active_start,
                "end": len(text),
            }
        )
    return spans


def _script_of(char: str) -> str | None:
    try:
        name = unicodedata.name(char)
    except ValueError:
        return None
    if name.startswith("CYRILLIC "):
        return "Cyrillic"
    if name.startswith("CJK UNIFIED IDEOGRAPH") or name.startswith("CJK COMPATIBILITY"):
        return "CJK"
    if name.startswith("HIRAGANA ") or name.startswith("KATAKANA "):
        return "CJK"
    if name.startswith("HANGUL "):
        return "Hangul"
    if name.startswith("THAI "):
        return "Thai"
    return None


def _snippet(text: str, start: int, end: int, radius: int = 48) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = text[left:right]
    return re.sub(r"\s+", " ", snippet).strip()
