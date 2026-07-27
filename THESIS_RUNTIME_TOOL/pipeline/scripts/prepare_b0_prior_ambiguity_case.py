from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b0_entity_prior_challenge_experiment import validate_prior_cards
from pipeline.literary.checkpoint import canonical_hash


class PrepareAmbiguityCaseError(RuntimeError):
    pass


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PrepareAmbiguityCaseError(f"cannot load JSON: {path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_ambiguity_case(
    *,
    base_prior_cards_path: Path,
    additional_card_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    base_raw = _load_json(base_prior_cards_path)
    base_cards = base_raw.get("prior_cards") if isinstance(base_raw, Mapping) else base_raw
    cards = validate_prior_cards(base_cards)
    additional_raw = _load_json(additional_card_path)
    additional = (
        additional_raw.get("prior_card")
        if isinstance(additional_raw, Mapping) and "prior_card" in additional_raw
        else additional_raw
    )
    if not isinstance(additional, Mapping):
        raise PrepareAmbiguityCaseError("additional card file must contain one object")
    combined = validate_prior_cards([*cards, dict(additional)])
    ids = [row["prior_card_id"] for row in combined]
    if len(ids) != len(set(ids)):
        raise PrepareAmbiguityCaseError("combined prior cards contain duplicate ids")
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "supplied_prior_cards.json", {"prior_cards": combined})
    report = {
        "schema_version": "b0_prior_ambiguity_prepared_case_v1",
        "base_prior_card_count": len(cards),
        "combined_prior_card_count": len(combined),
        "additional_prior_card_id": additional["prior_card_id"],
        "supplied_prior_cards_hash": canonical_hash(combined),
        "hidden_oracle_sent_to_model": False,
    }
    _write_json(output_dir / "preparation_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append one synthetic prior card for a bounded ambiguity probe."
    )
    parser.add_argument("--base-prior-cards", type=Path, required=True)
    parser.add_argument("--additional-card", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = prepare_ambiguity_case(
        base_prior_cards_path=args.base_prior_cards,
        additional_card_path=args.additional_card,
        output_dir=args.output_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
