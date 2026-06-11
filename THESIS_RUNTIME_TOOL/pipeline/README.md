# Thesis Runtime Pipeline

This package contains the thesis runtime code introduced after vendoring the AI-LAB tool. Per `THESIS_ARCHITECTURE_LOCK.md` section 8.7, new runtime work belongs here rather than in the donor `app/` package.

Layout:

- `ingest/`: source adapters and deterministic block extraction.
- `prepass/`: whole-book memory construction before freeze.
- `memory/`: SQLite schema base, migrations, and store initialization.
- `retrieval/`: exact, FTS/BM25, and vector retrieval adapters.
- `context/`: context pack assembly, token budgeting, and logging payloads.
- `agents/`: DB-free wrappers for World Builder, Narrative, Translator, and Critic LLM calls.
- `critic/`: deterministic Tier 1 checks and issue shaping.
- `runner/`: experiment and block-level state machine orchestration.
- `eval/`: run/eval persistence and metrics.
- `configs/`, `scripts/`, `tests/`: runtime configuration, helper scripts, and tests.
