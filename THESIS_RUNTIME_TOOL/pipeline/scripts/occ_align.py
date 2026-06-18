from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import load_llm_config
from pipeline.eval.occ_align import (
    DEFAULT_ALIGN_SEED,
    DEFAULT_EXPERIMENT_ID,
    DEFAULT_SIMALIGN_METHOD,
    DEFAULT_SIMALIGN_MODEL,
    align_independent,
    align_selfreport,
    confirm_token,
    hashes_for_blocks,
    load_frozen_translations,
    load_occ_inputs,
    load_translation_model,
    make_simalign_aligner,
    preview_selfreport,
    simalign_cache_key,
    write_jsonl,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Occurrence-level alignment pilot for frozen D2L outputs.")
    parser.add_argument("--method", choices=["simalign", "selfreport"], required=True)
    parser.add_argument("--config", choices=["S0", "S1"], required=True)
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--report", help="Accepted for CLI parity; not mutated.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--sample-csv", help="Optional proposer-blind gold sample CSV; align only listed occ_id rows for this config.")
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT_ID)
    parser.add_argument("--profile", default="technical_d2l_v1")
    parser.add_argument("--term-policy-root")
    parser.add_argument("--simalign-model", default=DEFAULT_SIMALIGN_MODEL)
    parser.add_argument("--simalign-method", default=DEFAULT_SIMALIGN_METHOD)
    parser.add_argument("--seed", type=int, default=DEFAULT_ALIGN_SEED)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--cache-dir", default="data/cache/occ_align")
    parser.add_argument("--llm-config", default="pipeline/configs/llm_translate.yaml")
    parser.add_argument("--llm-cache", default="data/cache/occ_align_selfreport.sqlite3")
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--confirm-token")
    args = parser.parse_args()

    resolved_chapter, blocks, occ_frame = load_occ_inputs(
        args.db,
        chapter=args.chapter,
        profile_name=args.profile,
        term_policy_root=args.term_policy_root,
    )
    blocks_by_id = {block.block_id: block.text for block in blocks}
    frozen_targets = load_frozen_translations(
        args.db,
        config=args.config,
        experiment_id=args.experiment,
    )
    frozen_targets = {block_id: frozen_targets.get(block_id, "") for block_id in blocks_by_id}
    if args.sample_csv:
        wanted = _sample_occ_ids(args.sample_csv, args.config)
        occ_frame = [item for item in occ_frame if item.occ_id in wanted]
        used_blocks = {item.block_id for item in occ_frame}
        blocks_by_id = {block_id: text for block_id, text in blocks_by_id.items() if block_id in used_blocks}
        frozen_targets = {block_id: text for block_id, text in frozen_targets.items() if block_id in used_blocks}

    if args.method == "simalign":
        rows = _run_simalign_with_block_cache(
            occ_frame=occ_frame,
            blocks_by_id=blocks_by_id,
            frozen_targets=frozen_targets,
            config=args.config,
            cache_dir=args.cache_dir,
            model=args.simalign_model,
            method=args.simalign_method,
            seed=args.seed,
            device=args.device,
        )
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(args.out, rows)
        print(
            json.dumps(
                {
                    "method": "simalign",
                    "chapter": resolved_chapter,
                    "config": args.config,
                    "occurrences": len(occ_frame),
                    "sample_csv": args.sample_csv,
                    "rows": len(rows),
                    "cache": "per_block",
                    "out": args.out,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    llm_config = load_llm_config(args.llm_config)
    translation_model = load_translation_model(
        args.db,
        config=args.config,
        experiment_id=args.experiment,
    )
    if llm_config.model != translation_model:
        raise SystemExit(
            "Self-report must use the same model that produced translation_runs: "
            f"{llm_config.model!r} != {translation_model!r}"
        )
    preview = preview_selfreport(
        occ_frame,
        blocks_by_id,
        frozen_targets,
        config=args.config,
        llm_config=llm_config,
    )
    preview["chapter"] = resolved_chapter
    preview["occurrences"] = len(occ_frame)
    preview["sample_csv"] = args.sample_csv
    if args.preview_only:
        print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    expected = confirm_token(
        "selfreport",
        args.config,
        llm_config.model,
        int(preview["calls"]),
        int(preview["estimated_prompt_tokens"]),
        int(preview["estimated_max_output_tokens"]),
    )
    if args.confirm_token != expected:
        raise SystemExit(
            "Refusing self-report LLM calls without matching confirm token. "
            f"Run --preview-only first and pass --confirm-token {expected}"
        )
    client = LLMClient(llm_config, args.llm_cache)
    proposals = align_selfreport(
        occ_frame,
        blocks_by_id,
        frozen_targets,
        config=args.config,
        client=client,
    )
    write_jsonl(args.out, proposals)
    print(json.dumps({**preview, "out": args.out, "rows": len(proposals)}, ensure_ascii=False, indent=2))
    return 0


def _sample_occ_ids(path: str | Path, config: str) -> set[str]:
    result: set[str] = set()
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        filtered = (line for line in fh if not line.startswith("#"))
        for row in csv.DictReader(filtered):
            if str(row.get("config") or "") == config and row.get("occ_id"):
                result.add(str(row["occ_id"]))
    if not result:
        raise SystemExit(f"No sample rows found for config={config} in {path}")
    return result


def _run_simalign_with_block_cache(
    *,
    occ_frame: list[object],
    blocks_by_id: dict[str, str],
    frozen_targets: dict[str, str],
    config: str,
    cache_dir: str | Path,
    model: str,
    method: str,
    seed: int,
    device: str,
) -> list[dict[str, object]]:
    from pipeline.eval.occ_align import read_jsonl

    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    occ_by_block: dict[str, list[object]] = {}
    for occ in occ_frame:
        block_id = getattr(occ, "block_id")
        occ_by_block.setdefault(block_id, []).append(occ)
    aligner = None
    rows: list[dict[str, object]] = []
    for block_id in sorted(occ_by_block):
        source_hash, target_hash = hashes_for_blocks(
            {block_id: blocks_by_id.get(block_id, "")},
            {block_id: frozen_targets.get(block_id, "")},
        )
        cache_key = simalign_cache_key(
            model=model,
            method=method,
            seed=seed,
            block_ids=[block_id],
            config=config,
            source_hash=source_hash,
            target_hash=target_hash,
        )
        cache_path = cache_root / f"{cache_key}.jsonl"
        wanted_occ_ids = {getattr(occ, "occ_id") for occ in occ_by_block[block_id]}
        if cache_path.exists():
            rows.extend(
                row for row in read_jsonl(cache_path)
                if str(row.get("occ_id")) in wanted_occ_ids
            )
            continue
        if aligner is None:
            aligner = make_simalign_aligner(model=model, device=device)
        block_rows = align_independent(
            occ_by_block[block_id],  # type: ignore[arg-type]
            {block_id: blocks_by_id.get(block_id, "")},
            {block_id: frozen_targets.get(block_id, "")},
            config=config,
            aligner=aligner,
            align_method=method,
            model=model,
            seed=seed,
        )
        write_jsonl(cache_path, block_rows)
        rows.extend(
            row for row in read_jsonl(cache_path)
            if str(row.get("occ_id")) in wanted_occ_ids
        )
    return sorted(rows, key=lambda row: (str(row.get("block_id")), str(row.get("occ_id")), str(row.get("config"))))


if __name__ == "__main__":
    raise SystemExit(main())
