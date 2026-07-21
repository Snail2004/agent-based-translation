from __future__ import annotations

from typing import Any, Mapping


SEMANTIC_BLOCK_KINDS = {"code", "formula", "list_item", "table"}


def _ok_arm(arms: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    arm = arms.get(name)
    if isinstance(arm, Mapping) and arm.get("status") == "ok":
        return arm
    return None


def _warnings(arm: Mapping[str, Any] | None) -> set[str]:
    if not arm:
        return set()
    metrics = arm.get("metrics") or {}
    return {str(value) for value in metrics.get("warnings") or []}


def _pairwise(
    pairwise: list[Mapping[str, Any]],
    left: str,
    right: str,
) -> Mapping[str, Any] | None:
    for item in pairwise:
        if item.get("left") == left and item.get("right") == right:
            return item
        if item.get("left") == right and item.get("right") == left:
            return {
                "token_coverage_left_by_right": item.get("token_coverage_right_by_left"),
                "token_coverage_right_by_left": item.get("token_coverage_left_by_right"),
                "ordered_shingle_coverage_left_by_right": item.get(
                    "ordered_shingle_coverage_right_by_left"
                ),
                "ordered_shingle_coverage_right_by_left": item.get(
                    "ordered_shingle_coverage_left_by_right"
                ),
            }
    return None


def _pandoc_semantic_kinds(arm: Mapping[str, Any] | None) -> list[str]:
    if not arm:
        return []
    kinds = (arm.get("metrics") or {}).get("block_kinds") or {}
    return sorted(kind for kind in SEMANTIC_BLOCK_KINDS if int(kinds.get(kind, 0)) > 0)


def recommend_benchmark_toolchain(source_report: Mapping[str, Any]) -> dict[str, Any]:
    """Return an advisory recommendation, never a production parser decision.

    The benchmark can expose a safe default and a review gate, but it cannot
    certify semantic chapter boundaries. Production wiring remains out of
    scope for this module.
    """

    source_format = str(source_report.get("source_format") or "")
    arms = source_report.get("arms") or {}
    pairwise = list(source_report.get("pairwise") or [])
    app = _ok_arm(arms, "app_current")
    pandoc = _ok_arm(arms, "pandoc")
    reasons: list[str] = []

    if source_format == "epub":
        app_low_confidence = "toc_low_confidence" in _warnings(app)
        overlap = _pairwise(pairwise, "app_current", "pandoc")
        pandoc_covered_by_app = (
            float(overlap.get("token_coverage_right_by_left", 0.0)) if overlap else None
        )
        if app and not app_low_confidence and (
            pandoc_covered_by_app is None or pandoc_covered_by_app >= 0.90
        ):
            reasons.append("The current EPUB parser reported a high-confidence ToC.")
            if pandoc_covered_by_app is not None:
                reasons.append(
                    f"It covered {pandoc_covered_by_app:.3f} of Pandoc lexical content; "
                    "the remainder may be front/back matter."
                )
            return {
                "primary": "app_current",
                "fallback": "pandoc",
                "structure_status": "chapter_candidates_from_epub_toc",
                "review_required": False,
                "reasons": reasons,
            }
        if pandoc:
            if app_low_confidence:
                reasons.append("The current EPUB parser reported toc_low_confidence.")
            if pandoc_covered_by_app is not None and pandoc_covered_by_app < 0.90:
                reasons.append(
                    f"The current parser covered only {pandoc_covered_by_app:.3f} of Pandoc lexical content."
                )
            reasons.append("Pandoc is the loss-avoidance fallback; its unit boundaries still require review.")
            return {
                "primary": "pandoc",
                "fallback": None,
                "structure_status": "chapter_candidates_require_review",
                "review_required": True,
                "reasons": reasons,
            }

    if source_format == "markdown" and pandoc:
        semantic_kinds = _pandoc_semantic_kinds(pandoc)
        if semantic_kinds:
            reasons.append(
                "Pandoc preserved structured block kinds: " + ", ".join(semantic_kinds) + "."
            )
        reasons.append("A corpus-specific loader remains preferable when its source contract is known.")
        fallback = "app_current" if app else None
        return {
            "primary": "pandoc",
            "fallback": fallback,
            "structure_status": "heading_candidates_or_document_unit",
            "review_required": bool((pandoc.get("metrics") or {}).get("document_unit_fallback")),
            "reasons": reasons,
        }

    if source_format == "html":
        overlap = _pairwise(pairwise, "app_current", "pandoc")
        pandoc_covered_by_app = (
            float(overlap.get("token_coverage_right_by_left", 0.0)) if overlap else None
        )
        if app and (pandoc_covered_by_app is None or pandoc_covered_by_app >= 0.90):
            reasons.append("The current HTML parser produced compact main-content blocks.")
            if pandoc_covered_by_app is not None:
                reasons.append(
                    f"It covered {pandoc_covered_by_app:.3f} of Pandoc lexical content."
                )
            return {
                "primary": "app_current",
                "fallback": "pandoc" if pandoc else None,
                "structure_status": "heading_candidates_or_document_unit",
                "review_required": bool((app.get("metrics") or {}).get("document_unit_fallback")),
                "reasons": reasons,
            }
        if pandoc:
            reasons.append("Pandoc retained more content than the current HTML parser.")
            return {
                "primary": "pandoc",
                "fallback": None,
                "structure_status": "heading_candidates_require_review",
                "review_required": True,
                "reasons": reasons,
            }

    if source_format == "txt" and pandoc:
        reasons.extend(
            [
                "Plain text has no reliable structural metadata.",
                "Pandoc preserves the text as blocks without claiming that a chapter exists.",
            ]
        )
        return {
            "primary": "pandoc",
            "fallback": None,
            "structure_status": "document_unit_unsegmented",
            "review_required": True,
            "reasons": reasons,
        }

    available = [name for name, arm in arms.items() if arm.get("status") == "ok"]
    return {
        "primary": None,
        "fallback": None,
        "structure_status": "no_safe_recommendation",
        "review_required": True,
        "reasons": [
            "No safe format-specific recommendation could be derived.",
            "Available benchmark arms: " + (", ".join(sorted(available)) or "none") + ".",
        ],
    }
