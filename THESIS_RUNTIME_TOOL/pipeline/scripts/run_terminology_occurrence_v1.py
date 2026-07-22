from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.terminology_occurrence_v1 import (
    build_terminology_occurrence_metrics_v1,
    persist_terminology_occurrence_metrics_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute deterministic D2L-profile TC-Occ and TA-Occ metrics."
    )
    parser.add_argument("--d2l-package", required=True, type=Path)
    parser.add_argument(
        "--cascade",
        required=True,
        action="append",
        metavar="ARM=PATH",
        help="One sealed localization/cascade artifact per translation arm.",
    )
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--producer-code-commit", required=True)
    args = parser.parse_args()

    package_path = args.d2l_package.resolve()
    package = _load_json(package_path)
    cascade_paths = _parse_arm_paths(args.cascade)
    payloads: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, str]] = {}
    for arm_id, path in cascade_paths.items():
        resolved = path.resolve()
        payloads[arm_id] = _load_json(resolved)
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        artifacts[arm_id] = {
            "artifact_id": f"d2l-cascade-{arm_id.casefold()}-{digest[:16]}",
            "artifact_sha256": digest,
        }

    artifact = build_terminology_occurrence_metrics_v1(
        package,
        payloads,
        artifacts,
        generated_at=args.generated_at,
        producer_code_commit=args.producer_code_commit,
    )
    persisted = persist_terminology_occurrence_metrics_v1(
        output_root=args.output_root,
        artifact_payload=artifact,
    )
    summary = {
        "status": "complete",
        "api_calls": 0,
        "artifact_path": str(persisted.path),
        "artifact_sha256": artifact["integrity"]["artifact_sha256"],
        "reused": persisted.reused,
        "arms": {
            arm_id: {
                "tc_occ": row["tc_occ"]["value"],
                "ta_occ": row["ta_occ"]["value_lower"],
                "source_occurrences": row["source_occurrence_count"],
            }
            for arm_id, row in artifact["arms"].items()
        },
        "comparison": artifact["comparison"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _parse_arm_paths(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for index, value in enumerate(values):
        arm_id, separator, raw_path = value.partition("=")
        if not separator or not arm_id.strip() or not raw_path.strip():
            raise ContractValidationError(
                "cascade_argument",
                f"$.cascade[{index}]",
                "expected ARM=PATH",
            )
        arm = arm_id.strip()
        if arm in result:
            raise ContractValidationError(
                "duplicate", f"$.cascade[{index}]", "arm was supplied more than once"
            )
        result[arm] = Path(raw_path.strip())
    return result


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractValidationError("missing_artifact", str(path), "file does not exist")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError("invalid_json", str(path), str(exc)) from exc
    if not isinstance(value, dict):
        raise ContractValidationError("type", str(path), "expected a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
