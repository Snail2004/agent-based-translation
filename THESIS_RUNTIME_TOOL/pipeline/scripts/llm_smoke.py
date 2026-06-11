from __future__ import annotations

import os
from pathlib import Path

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import load_llm_config


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set; skipping real smoke call.")
        return 0

    pipeline_root = Path(__file__).resolve().parents[1]
    config = load_llm_config(pipeline_root / "configs" / "llm_default.yaml")
    cache_path = pipeline_root / ".cache" / "llm_smoke_cache.sqlite3"
    client = LLMClient(config=config, cache_path=cache_path)
    result = client.call(
        [{"role": "user", "content": "Return exactly: ok"}],
        tag="manual_smoke",
    )
    print(
        {
            "text": result.text,
            "model": result.model,
            "system_fingerprint": result.system_fingerprint,
            "usage": result.usage.total_billable_tokens,
            "cost_usd": result.cost_usd,
            "from_cache": result.from_cache,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
