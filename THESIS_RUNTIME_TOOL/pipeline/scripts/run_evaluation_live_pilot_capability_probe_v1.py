from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from pipeline.eval.live_pilot_capability_probe_v1 import (
    build_clean_evaluation_probe_implementation_binding_v1,
)
from pipeline.eval.live_pilot_capability_run_v1 import (
    run_evaluation_capability_probes_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
)
from pipeline.llm_backend import (
    MappingCredentialProvider,
    UrllibTransportSender,
    canonical_json,
    credential_commitment,
)


def load_selected_google_credential_v1(
    path: Path, *, physical_row: int, expected_row_count: int
) -> str:
    if expected_row_count < 1:
        raise ValueError("expected_row_count must be positive")
    if physical_row < 1 or physical_row > expected_row_count:
        raise ValueError("physical_row is outside the declared key file")
    values = [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if len(values) != expected_row_count:
        raise ValueError(
            f"credential file must contain exactly {expected_row_count} non-empty rows"
        )
    credential = values[physical_row - 1]
    if any(character.isspace() for character in credential) or len(credential) < 20:
        raise ValueError("selected credential row is malformed")
    return credential


def build_official_google_source_v1(
    *,
    source_id: str,
    source_revision: str,
    credential_ref: str,
    physical_quota_bucket_id: str,
    credential: str,
) -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": source_id,
        "source_revision": source_revision,
        "source_class": "remote_api",
        "adapter_id": "google_genai_rest_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "models_generate_content",
        "endpoint_class": "remote",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": credential_ref,
        "credential_commitment": credential_commitment(credential),
        "physical_quota_bucket_id": physical_quota_bucket_id,
        "enabled": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.execute_live:
        parser.error("--execute-live is required; this runner has no implicit live mode")

    credential = load_selected_google_credential_v1(
        args.keys_file,
        physical_row=args.physical_row,
        expected_row_count=args.expected_row_count,
    )
    source = build_official_google_source_v1(
        source_id=args.source_id,
        source_revision=args.source_revision,
        credential_ref=args.credential_ref,
        physical_quota_bucket_id=args.physical_quota_bucket_id,
        credential=credential,
    )
    models = {
        SF_BT_BACK_TRANSLATOR_ROLE_ID: args.back_translator_model,
        SF_BT_SEMANTIC_JUDGE_ROLE_ID: args.semantic_judge_model,
        PJ_JUDGE_ROLE_ID: args.pj_judge_model,
    }
    accepted = {role_id: [model_id] for role_id, model_id in models.items()}
    binding = build_clean_evaluation_probe_implementation_binding_v1()
    summary = run_evaluation_capability_probes_v1(
        source=source,
        requested_models_by_role=models,
        accepted_observed_models_by_role=accepted,
        credential_provider=MappingCredentialProvider(
            {args.credential_ref: credential}
        ),
        output_root=args.output_root,
        probe_run_prefix=args.probe_run_prefix,
        issued_at_utc=args.issued_at_utc,
        implementation_binding=binding,
        sender=UrllibTransportSender(),
    )
    public_result = {
        "status": summary["status"],
        "summary_path": str((args.output_root / "run_summary.json").resolve()),
        "attempted_roles": [
            {
                "role_id": row["role_id"],
                "requested_model_id": row["requested_model_id"],
                "status": row["status"],
            }
            for row in summary["attempted_roles"]
        ],
        "halt": summary["halt"],
    }
    print(canonical_json(public_result))
    return 0 if summary["status"] == "qualified" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualify the three Evaluation live-pilot schemas on one official Gemini row."
    )
    parser.add_argument("--keys-file", type=Path, required=True)
    parser.add_argument("--physical-row", type=int, required=True)
    parser.add_argument("--expected-row-count", type=int, default=5)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--credential-ref", required=True)
    parser.add_argument("--physical-quota-bucket-id", required=True)
    parser.add_argument("--back-translator-model", required=True)
    parser.add_argument("--semantic-judge-model", required=True)
    parser.add_argument("--pj-judge-model", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--probe-run-prefix", required=True)
    parser.add_argument("--issued-at-utc", required=True)
    parser.add_argument("--execute-live", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
