"""CLI for the versioned D2L component package recovery transaction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.prepass.d2l_component_package_recovery_v1 import (
    D2LComponentPackageRecoveryError,
    D2LComponentPackageRecoveryRequestV1,
    recover_d2l_component_package_v1,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component-root", type=Path, required=True)
    parser.add_argument("--transaction-root", type=Path, required=True)
    parser.add_argument(
        "--authoritative-index-file",
        type=Path,
        required=True,
    )
    parser.add_argument("--authoritative-index-sha256", required=True)
    parser.add_argument("--parent-snapshot-ref", required=True)
    parser.add_argument("--parent-snapshot-sha256", required=True)
    parser.add_argument("--parent-import-ordinal", type=int)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-events-sha256", required=True)
    parser.add_argument("--expected-broken-index-sha256", required=True)
    parser.add_argument("--expected-manifest-temp-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    request = D2LComponentPackageRecoveryRequestV1(
        component_root=args.component_root,
        transaction_root=args.transaction_root,
        authoritative_index_file=args.authoritative_index_file,
        authoritative_index_sha256=args.authoritative_index_sha256,
        parent_snapshot_ref=args.parent_snapshot_ref,
        parent_snapshot_sha256=args.parent_snapshot_sha256,
        parent_import_ordinal=args.parent_import_ordinal,
        expected_manifest_sha256=args.expected_manifest_sha256,
        expected_events_sha256=args.expected_events_sha256,
        expected_broken_index_sha256=args.expected_broken_index_sha256,
        expected_manifest_temp_sha256=(
            args.expected_manifest_temp_sha256
        ),
    )
    try:
        receipt = recover_d2l_component_package_v1(request)
    except D2LComponentPackageRecoveryError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "status": "recovered",
                "transaction_id": receipt["transaction_id"],
                "receipt_sha256": receipt["receipt_sha256"],
                "component_attempt_id": receipt["component_attempt_id"],
                "provider_call_count": receipt["provider_call_count"],
                "post_package_validation_sha256": receipt[
                    "post_package_validation_sha256"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
