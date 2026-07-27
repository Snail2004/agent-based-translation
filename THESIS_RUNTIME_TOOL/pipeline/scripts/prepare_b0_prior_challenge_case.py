from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Sequence

from pipeline.literary.b0_entity_prior_challenge_experiment import (
    build_hidden_corruption_manifest,
    validate_prior_cards,
)


class PrepareCaseError(RuntimeError):
    pass


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PrepareCaseError(f"cannot load JSON: {path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_case(
    *,
    correct_prior_cards_path: Path,
    prior_card_id: str,
    field: str,
    replacement_json: str | None,
    replacement_string: str | None,
    expected_issue_code: str,
    mutation_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    source = _load_json(correct_prior_cards_path)
    raw_cards = source.get("prior_cards") if isinstance(source, dict) else source
    correct = validate_prior_cards(raw_cards)
    supplied = deepcopy(correct)
    matches = [row for row in supplied if row["prior_card_id"] == prior_card_id]
    if len(matches) != 1:
        raise PrepareCaseError("prior_card_id must resolve to exactly one card")
    if field not in matches[0]:
        raise PrepareCaseError("mutation field is not present on the prior card")
    if (replacement_json is None) == (replacement_string is None):
        raise PrepareCaseError(
            "exactly one of replacement-json or replacement-string is required"
        )
    if replacement_string is not None:
        replacement: Any = replacement_string
    else:
        try:
            replacement = json.loads(str(replacement_json))
        except (ValueError, TypeError) as exc:
            raise PrepareCaseError("replacement-json must be valid JSON") from exc
    matches[0][field] = replacement
    supplied = validate_prior_cards(supplied)
    manifest = build_hidden_corruption_manifest(
        mutation_id=mutation_id,
        correct_prior_cards=correct,
        supplied_prior_cards=supplied,
        expected_issue_code=expected_issue_code,
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "correct_prior_cards.json", {"prior_cards": correct})
    _write_json(output_dir / "supplied_prior_cards.json", {"prior_cards": supplied})
    _write_json(output_dir / "hidden_corruption_manifest.json", manifest)
    report = {
        "schema_version": "b0_prior_challenge_prepared_case_v1",
        "mutation_id": mutation_id,
        "prior_card_id": prior_card_id,
        "changed_field": field,
        "expected_issue_code": expected_issue_code,
        "hidden_manifest_sent_to_model": False,
        "correct_prior_cards_path": str(correct_prior_cards_path),
    }
    _write_json(output_dir / "preparation_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare one isolated hidden B0 prior-card mutation."
    )
    parser.add_argument("--correct-prior-cards", type=Path, required=True)
    parser.add_argument("--prior-card-id", required=True)
    parser.add_argument("--field", required=True)
    replacement = parser.add_mutually_exclusive_group(required=True)
    replacement.add_argument("--replacement-json")
    replacement.add_argument("--replacement-string")
    parser.add_argument("--expected-issue-code", required=True)
    parser.add_argument("--mutation-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = prepare_case(
        correct_prior_cards_path=args.correct_prior_cards,
        prior_card_id=args.prior_card_id,
        field=args.field,
        replacement_json=args.replacement_json,
        replacement_string=args.replacement_string,
        expected_issue_code=args.expected_issue_code,
        mutation_id=args.mutation_id,
        output_dir=args.output_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
