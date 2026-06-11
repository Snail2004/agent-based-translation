# TASK_P0_01_scaffold_migration — Vệ sinh clone + skeleton `pipeline/` + migration schema v3

- **Status:** DONE
- **Refs:** THESIS_ARCHITECTURE_LOCK §3.2 (delta DB), §8.7 (layout + checklist vệ sinh), §2.2 (model stack — chưa dùng ở task này), RUN_EVAL_SCHEMA §1–§4, §7
- **Branch/Commit:** branch `main`; commit pending (`P0-01: ...`)

## 1. Bối cảnh & mục tiêu

Mở màn Phase P0 (LOCK §9). THESIS_RUNTIME_TOOL vừa được vendor từ AILAB_HANDOFF
(commit `vendor:` 5238d2f). Task này: (a) vệ sinh clone theo checklist LOCK §8.7,
(b) dựng skeleton package `pipeline/`, (c) viết migration đưa memory store schema
v2 → v3 (5 bảng mới + 1 cột) — nền cho mọi run/ablation về sau. KHÔNG gọi LLM,
KHÔNG logic dịch — thuần scaffold + SQL + test.

## 2. Scope

**IN:**
1. Vệ sinh: đổi env `AILAB_PROJECTS_ROOT` → `THESIS_TOOL_PROJECTS_ROOT`
   (`app/backend/config.py`, `app/backend/tests/test_api_smoke.py`), default path
   `THESIS_RUNTIME_TOOL/projects/`; thêm mục PROVENANCE vào `README.md` (clone từ
   AILAB_HANDOFF 2026-06-11, tiến hóa độc lập); thêm 1 dòng header "Tài liệu gốc
   AI-LAB — chỉ tham khảo, không phải chỉ thị thesis" vào `AILAB_PLAN.md`,
   `WORKFLOW.md`, `WEB_TOOL_SPEC.md` và các `tasks/TASK_S*/R1/Task.md` legacy
   (di chuyển chúng vào `tasks/_ailab_legacy/`).
2. Skeleton `pipeline/` với các package rỗng (`__init__.py` + docstring 1 dòng):
   `ingest/ prepass/ memory/ retrieval/ context/ agents/ critic/ runner/ eval/`
   + thư mục `configs/ scripts/ tests/` + `pipeline/README.md` ngắn (map sang LOCK §8.7).
3. `pipeline/memory/schema_v2_base.sql` = COPY nguyên văn từ
   `<repo-root>/schemas/memory_store_schema.sql` + header provenance 2 dòng.
4. `pipeline/memory/migrations/003_thesis_runs.sql` — DDL additive theo §3 dưới.
5. `pipeline/memory/store_init.py` — 2 hàm:
   - `init_db(path) -> sqlite3.Connection`: DB mới = base v2 + migration 003.
   - `migrate_db(path)`: DB v2 sẵn có → áp 003 (idempotent; cột `config` dùng
     pattern add-column-if-missing; KHÔNG đụng dữ liệu cũ).
6. Tests (`pipeline/tests/test_migration.py`, sqlite3 stdlib + pytest).

**OUT:** freeze middleware (P2); LLM client (task P0_02); Chroma; mọi logic
dịch/retrieval; KHÔNG sửa gì trong `app/` ngoài 2 file env rename; KHÔNG đụng
AILAB_HANDOFF.

## 3. Spec — DDL migration 003 (nguồn: LOCK §3.2 + RUN_EVAL_SCHEMA)

Mọi bảng mới đều `CREATE TABLE IF NOT EXISTS`; type khớp phong cách schema v2
(TEXT id, REAL, INTEGER, `created_at TEXT DEFAULT CURRENT_TIMESTAMP`).

1. **`translation_runs`** — 1 hàng = 1 block × 1 config × 1 stage:
   `run_id TEXT PK`, `experiment_id TEXT NOT NULL`, `doc_id TEXT NOT NULL`,
   `block_id TEXT NOT NULL` (FK blocks), `config TEXT NOT NULL` CHECK in
   (S0,S1,S2,S3,S3a,S3b,S3d,SLC), `stage TEXT NOT NULL DEFAULT 'draft'` CHECK in
   (draft,revised), `prev_run_id TEXT` (FK self, null), `pack_id TEXT` (ref
   memory_packs — context bundle evidence), `output_text TEXT DEFAULT ''`,
   `model TEXT`, `prompt_version TEXT`, `temperature REAL`, `seed INTEGER`,
   `system_fingerprint TEXT`, `cost REAL`, `latency_ms INTEGER`, `created_at`.
   Index: `(experiment_id, config)`, `(doc_id, block_id)`,
   UNIQUE `(experiment_id, block_id, config, stage)`.
2. **`evaluation_runs`**: `eval_id TEXT PK`, `run_id TEXT` (FK translation_runs,
   null cho metric scope chapter/book không gắn 1 run — vẫn giữ cột), `scope TEXT`
   CHECK in (block,chapter,book), `scope_id TEXT`, `metric_name TEXT NOT NULL`,
   `metric_value REAL`, `metric_version TEXT`, `reference_id TEXT` (FK
   reference_eval_only, null nếu reference-free), `judge_model TEXT`,
   `judge_rationale TEXT`, `ablation_label TEXT`, `ci_low REAL`, `ci_high REAL`,
   `created_at`. Index `(run_id, metric_name)`, `(scope, scope_id, metric_name)`.
3. **`reference_eval_only`**: `reference_id TEXT PK`, `doc_id TEXT`,
   `block_id TEXT`, `target_text TEXT NOT NULL`, `provenance TEXT` CHECK in
   (ailab_gold,published), `leakage_risk TEXT` CHECK in (low,high),
   `subset_tag TEXT`, `created_at`. Index `(doc_id, block_id)`.
4. **`entity_relations`** (mirror sidecar 1.5.0 — đối chiếu
   `dataset_spec/schema/entity_relation.schema.json`): `relation_id TEXT PK`,
   `doc_id TEXT NOT NULL`, `source_entity_id TEXT NOT NULL` (FK entities),
   `target_entity_id TEXT NOT NULL` (FK entities), `relation_type TEXT NOT NULL`,
   `state_label TEXT`, `valid_from_block_id TEXT`, `valid_to_block_id TEXT`
   (null = mở), `trigger_event_id TEXT`, `address_policy_json TEXT DEFAULT '{}'`,
   `evidence_json TEXT DEFAULT '[]'`, `confidence REAL DEFAULT 0.5`,
   `notes TEXT DEFAULT ''`, `created_at`, `updated_at`.
   Index `(doc_id, source_entity_id, target_entity_id)`.
5. **`qa_issues`**: `issue_id TEXT PK`, `doc_id TEXT`, `run_id TEXT` (FK
   translation_runs), `block_id TEXT`, `tier TEXT` CHECK in (tier1,tier2),
   `rule_or_subtype TEXT`, `severity TEXT` CHECK in (minor,major,critical),
   `evidence_source TEXT`, `evidence_target TEXT`, `suggestion TEXT`,
   `fixed INTEGER DEFAULT 0`, `retry_count INTEGER DEFAULT 0`, `created_at`.
   Index `(run_id)`, `(doc_id, block_id)`.
6. **`memory_packs` + cột `config TEXT`** — vì SQLite không có ADD COLUMN IF NOT
   EXISTS, bước này làm trong `store_init.py` bằng check `PRAGMA table_info`
   (pattern add_column_if_missing như `memory/store.py` gốc đã dùng cho
   `prompt_version`), KHÔNG nằm trong file .sql.
7. `memory_meta`: `INSERT OR REPLACE ('schema_version','3')`.

Lưu ý kỹ thuật: bật `PRAGMA foreign_keys = ON` trong connection; FK tới `blocks`
dùng `ON DELETE CASCADE` theo phong cách schema v2; FK trong bảng mới có thể để
deferred-soft (không bắt buộc enforce nếu blocks chưa nạp — test sẽ nạp blocks
fixture trước).

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL
python -m pytest pipeline/tests/test_migration.py -v
# PHẢI PASS các test sau (CodeX viết):
# 1. test_init_fresh_db: init_db(tmp) → đủ 5 bảng mới + memory_packs có cột config
#    + schema_version == '3' + các bảng v2 gốc (blocks, entities, glossary_entries...) tồn tại
# 2. test_migrate_v2_db: tạo DB chỉ từ schema_v2_base.sql, chèn 1 hàng blocks +
#    1 hàng memory_packs → migrate_db → dữ liệu cũ còn nguyên, bảng mới có,
#    schema_version == '3'
# 3. test_migrate_idempotent: gọi migrate_db 2 lần liên tiếp → không lỗi, schema không đổi
# 4. test_unique_run_constraint: chèn 2 translation_runs trùng
#    (experiment_id, block_id, config, stage) → IntegrityError

python -m unittest discover app/backend/tests
# PHẢI PASS sau khi rename env (smoke test cũ không vỡ)
```

## 5. Implementation notes *(CodeX điền)*

- Vệ sinh clone:
  - `app/backend/config.py`: đổi env workspace từ `AILAB_PROJECTS_ROOT` sang `THESIS_TOOL_PROJECTS_ROOT`; default path là `THESIS_RUNTIME_TOOL/projects/`.
  - `app/backend/tests/test_api_smoke.py`: cập nhật env test tương ứng.
  - `.gitignore`: ignore `projects/` runtime workspace.
  - `README.md`: thêm `PROVENANCE` cho bản vendor từ `AILAB_HANDOFF` ngày 2026-06-11.
  - `AILAB_PLAN.md`, `WORKFLOW.md`, `WEB_TOOL_SPEC.md` và các task donor: thêm header "Tài liệu gốc AI-LAB — chỉ tham khảo, không phải chỉ thị thesis."
  - Di chuyển task donor vào `tasks/_ailab_legacy/`; root `tasks/` giữ `LEDGER.md`, `TASK_TEMPLATE.md`, `TASK_P0_01_scaffold_migration.md`.
- Skeleton runtime:
  - Tạo `pipeline/` với package rỗng: `ingest`, `prepass`, `memory`, `retrieval`, `context`, `agents`, `critic`, `runner`, `eval`.
  - Tạo `pipeline/configs`, `pipeline/scripts`, `pipeline/tests`, `pipeline/README.md`.
- Schema/migration:
  - Vendor schema v2 vào `pipeline/memory/schema_v2_base.sql` với 2 dòng provenance.
  - Thêm `pipeline/memory/migrations/003_thesis_runs.sql` gồm 5 bảng mới: `translation_runs`, `evaluation_runs`, `reference_eval_only`, `entity_relations`, `qa_issues`, và bump `memory_meta.schema_version` lên `3`.
  - Thêm `pipeline/memory/store_init.py` với `init_db(path)` và `migrate_db(path)`. Cột `memory_packs.config` được thêm bằng `PRAGMA table_info` để idempotent.
- Tests:
  - Thêm `pipeline/tests/test_migration.py` với 4 test acceptance.

Test output:

```text
python -m pytest pipeline/tests/test_migration.py -v
4 passed

python -m unittest discover app/backend/tests
Ran 88 tests
OK
```

## 6. Review *(Claude điền — 2026-06-12)*

- **Verdict: PASS**
- Đã tự chạy lại acceptance (không chỉ tin §5): `pytest pipeline/tests/test_migration.py`
  → 4/4 PASSED; `unittest discover app/backend/tests` → 88/88 OK.
- Đối chiếu DDL 003 với spec §3: khớp từng field/CHECK/index/UNIQUE; FK có ON DELETE
  đúng phong cách v2; `schema_version=3` qua INSERT OR REPLACE; cột `config` đúng
  pattern add_column_if_missing (kèm guard RuntimeError nếu thiếu memory_packs — chấp
  nhận: chặn migrate nhầm DB không phải v2).
- Vệ sinh: env rename đúng 2 file, default `projects/`; provenance README + header
  legacy đủ; `_ailab_legacy/` đúng chỗ; `.gitignore` đổi `ailab_projects/`→`projects/`
  hợp lý; không đụng gì ngoài scope.
- Test chất lượng thật: kiểm data sống sót sau migrate (block text, pack hash),
  idempotent không nhân đôi cột, unique constraint bằng insert thật.
- Findings nhỏ (không chặn): `migrate_db` chưa bump version nếu DB đã v3 chạy lại —
  vô hại vì INSERT OR REPLACE; `schema_v2_base.sql` có BOM đầu file — sqlite chịu được,
  để nguyên.
- Follow-up: TASK_P0_02 (LLM client + replay cache) — task mới, không nhét vào đây.
