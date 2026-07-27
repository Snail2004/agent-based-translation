from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b0_entity_prior_challenge_experiment import (
    build_hidden_corruption_set_manifest,
    validate_prior_cards,
)


class PrepareStressCaseError(RuntimeError):
    pass


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PrepareStressCaseError(f"cannot load JSON: {path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_stress_case(
    *,
    base_prior_cards_path: Path,
    additional_card_path: Path,
    stress_plan_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    base_raw = _load_json(base_prior_cards_path)
    base = base_raw.get("prior_cards") if isinstance(base_raw, Mapping) else base_raw
    base_cards = validate_prior_cards(base)
    additional_raw = _load_json(additional_card_path)
    additional = (
        additional_raw.get("prior_card")
        if isinstance(additional_raw, Mapping) and "prior_card" in additional_raw
        else additional_raw
    )
    if not isinstance(additional, Mapping):
        raise PrepareStressCaseError("additional card file must contain one object")
    correct = validate_prior_cards([*base_cards, dict(additional)])
    supplied = deepcopy(correct)
    supplied_by_id = {row["prior_card_id"]: row for row in supplied}

    plan = _load_json(stress_plan_path)
    if not isinstance(plan, Mapping):
        raise PrepareStressCaseError("stress plan must be an object")
    mutations = plan.get("mutations")
    expected = plan.get("expected_outcomes")
    if not isinstance(mutations, list) or not isinstance(expected, list):
        raise PrepareStressCaseError("stress plan lists are missing")
    for mutation in mutations:
        if not isinstance(mutation, Mapping):
            raise PrepareStressCaseError("stress mutation must be an object")
        card_id = str(mutation.get("prior_card_id") or "")
        field = str(mutation.get("field") or "")
        card = supplied_by_id.get(card_id)
        if card is None or field not in card or "replacement" not in mutation:
            raise PrepareStressCaseError("stress mutation target is invalid")
        card[field] = deepcopy(mutation["replacement"])
    supplied = validate_prior_cards(supplied)
    manifest = build_hidden_corruption_set_manifest(
        mutation_id=str(plan.get("mutation_id") or ""),
        correct_prior_cards=correct,
        supplied_prior_cards=supplied,
        expected_outcomes=expected,
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "correct_prior_cards.json", {"prior_cards": correct})
    _write_json(output_dir / "supplied_prior_cards.json", {"prior_cards": supplied})
    _write_json(output_dir / "hidden_corruption_manifest.json", manifest)
    report = {
        "schema_version": "b0_prior_challenge_prepared_stress_v1",
        "mutation_id": manifest["mutation_id"],
        "prior_card_count": len(supplied),
        "changed_prior_card_count": len(manifest["changed_prior_cards"]),
        "expected_outcome_count": len(manifest["expected_outcomes"]),
        "hidden_manifest_sent_to_model": False,
    }
    _write_json(output_dir / "preparation_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a bounded multi-error B0 prior-card stress case."
    )
    parser.add_argument("--base-prior-cards", type=Path, required=True)
    parser.add_argument("--additional-card", type=Path, required=True)
    parser.add_argument("--stress-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = prepare_stress_case(
        base_prior_cards_path=args.base_prior_cards,
        additional_card_path=args.additional_card,
        stress_plan_path=args.stress_plan,
        output_dir=args.output_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
