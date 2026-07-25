"""CLI for trusted-prefix D2L journal race recovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.prepass.d2l_component_journal_recovery_v1 import (
    D2LComponentJournalRecoveryError,
    D2LComponentJournalRecoveryRequestV1,
    recover_d2l_component_journal_v1,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--request-json",
        "--request-file",
        dest="request_json",
        type=Path,
        required=True,
        help="UTF-8 JSON request using d2l_component_journal_recovery_request_v1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        raw = json.loads(args.request_json.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise D2LComponentJournalRecoveryError(
                "journal recovery request must be a JSON object"
            )
        request = D2LComponentJournalRecoveryRequestV1.from_mapping(raw)
        receipt = recover_d2l_component_journal_v1(request)
    except (
        D2LComponentJournalRecoveryError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {"status": "rejected", "error": str(exc)},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
