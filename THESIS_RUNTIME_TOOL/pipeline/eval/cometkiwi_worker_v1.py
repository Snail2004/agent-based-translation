from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any

from pipeline.eval.cometkiwi_subprocess_v1 import (
    COMETKIWI_BATCH_RESPONSE_SCHEMA_ID,
    COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID,
    validate_cometkiwi_batch_request_v1,
    validate_cometkiwi_batch_response_v1,
)


_MAX_STDIN_BYTES = 16 * 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description="Pinned local COMET-QE worker")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args()
    try:
        checkpoint = _logical_checkpoint_path(args.checkpoint)
        if args.describe:
            _emit(
                {
                    "schema_id": COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID,
                    "package_name": "unbabel-comet",
                    "package_version": _package_version("unbabel-comet"),
                    "python_version": platform.python_version(),
                    "device": "cpu",
                    "checkpoint_sha256": _sha256_file(checkpoint),
                }
            )
            return 0
        if args.batch_size < 1 or args.batch_size > 512:
            raise ValueError("batch size outside 1..512")
        encoded = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
        if len(encoded) > _MAX_STDIN_BYTES:
            raise ValueError("request exceeds worker input cap")
        request = validate_cometkiwi_batch_request_v1(_load_request(encoded))
        from comet import load_from_checkpoint

        model = load_from_checkpoint(str(checkpoint))
        output = model.predict(
            request["rows"],
            batch_size=args.batch_size,
            gpus=0,
            progress_bar=False,
        )
        response = validate_cometkiwi_batch_response_v1(
            {
                "schema_id": COMETKIWI_BATCH_RESPONSE_SCHEMA_ID,
                "scores": [float(score) for score in output.scores],
            },
            expected_count=len(request["rows"]),
        )
        _emit(response)
        return 0
    except Exception as exc:
        error = {
            "schema_id": "CometKiwiWorkerErrorV1",
            "error_type": type(exc).__name__,
        }
        sys.stderr.write(json.dumps(error, sort_keys=True) + "\n")
        return 2


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _load_request(encoded: bytes) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    value = json.loads(
        encoded.decode("utf-8"),
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise ValueError("request root is not an object")
    return value


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError as exc:
        raise RuntimeError("required package is unavailable") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _logical_checkpoint_path(value: str) -> Path:
    checkpoint = Path(os.path.abspath(Path(value).expanduser()))
    if not checkpoint.is_file():
        raise ValueError("checkpoint is not a file")
    return checkpoint


if __name__ == "__main__":
    raise SystemExit(main())
