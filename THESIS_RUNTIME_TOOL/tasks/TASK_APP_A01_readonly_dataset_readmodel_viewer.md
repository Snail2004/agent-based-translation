# TASK_APP_A01_readonly_dataset_readmodel_viewer — Read-only DatasetReadModel adapter + viewer + quarantine AILAB gold-authoring

- **Status:** DONE
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (nn) [chính], (ll) prompt-artifact-review, (kk) trục D; Directional-Lock (gold eval-only KHÔNG inject)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

Chốt 3 bên LOCK (nn): App khóa luận = **research cockpit** quan sát pipeline (engine headless), KHÔNG phải app dịch. App AILAB hiện tại là dataset-tool annotation gold (workflow Upload→Extract→Annotate→Validate→Export, README §8). Frontend React-UMD + Babel-standalone (0 build) + backend Flask blueprint **rất dễ tái dùng**; nhưng nó đọc workspace `*.jsonl`, còn pipeline ghi `memory.sqlite3` → **seam = SQLite → UI read-model**.

`APP_A01` là bước ĐẦU, gọn nhất: dựng **read-only DatasetReadModel adapter** từ SQLite của pipeline → shape viewer, **provenance tách cấu trúc**, **quarantine** lớp gold-authoring AILAB. **0 API, 0 pipeline change, 0 engine change.** Đây KHÔNG phải "cockpit đầy đủ" — observability (prompt/cache/cost) là `APP_B01`.

## 2. Scope

- **IN:**
  - **DatasetReadModel adapter**: đọc `data/jobs/<job>/memory.sqlite3` (chế độ read-only) → response shape:
    ```json
    {
      "meta": {"experiment_id": "...", "config": "...", "stage": "...", "prompt_version": "...", "available_runs": []},
      "runtime_memory": {"glossary_entries": [], "entities": [], "entity_relations": []},
      "eval_only": {"gold_glossary": [], "references": []},
      "translations": {"S0": [], "S1": []}
    }
    ```
    Metadata chọn run TỐI THIỂU (experiment_id/config/stage/prompt_version) — **KHÔNG** prompt/cache/cost chi tiết (→ B01).
  - **Endpoint mới** (Flask blueprint riêng, vd `GET /thesis/datasets/<job>`), tách khỏi `/projects/...` của AILAB.
  - **Trỏ viewer** (parts_left block·chapter·preview, parts_right glossary/entity/relation) vào read-model; preview = source vs translations S0/S1.
  - **Quarantine** AILAB gold-authoring (annotation / normalize / package / references) sau feature-flag (vd `THESIS_APP_MODE=cockpit`) hoặc route-group `ailab_legacy` — ẩn route + nút, **KHÔNG xóa file**.
  - **Provenance badge** lái bằng nhánh structural: `runtime_memory`=agent-built; `eval_only`=gold/oracle eval-only; (human-override nếu sau này có).
- **OUT:**
  - Observability: Prompt/Context Inspector + API calls + cache + token + cost = **`APP_B01`**.
  - Score/report + Consistency/Drift = **`APP_D01`**.
  - Run control / live-stream = **`APP_C01`**.
  - **Human-edit WRITE lane** (A01 READ-ONLY; ghi tay = task sau, đi lane riêng có log).
  - KHÔNG ghi frozen memory; KHÔNG đổi pipeline/engine/schema; KHÔNG **xóa** route AILAB (chỉ quarantine).

## 3. Spec *(Claude viết)*

**3.1 Adapter** — module mới (vd `app/backend/services/thesis_readmodel.py`), mở SQLite **read-only** (`sqlite3` `mode=ro` URI). Map bảng → nhánh:
- `glossary_entries` → `runtime_memory.glossary_entries`
- `entities` → `runtime_memory.entities`
- `entity_relations` → `runtime_memory.entity_relations`
- `translation_runs` (lọc theo experiment_id/stage) → `translations.{stage}`
- `eval_glossary_gold` → `eval_only.gold_glossary`
- `reference_eval_only` → `eval_only.references`
- **GUARD (Directional-Lock cấp-query):** adapter **CẤM** join/đưa `eval_glossary_gold`/`reference_eval_only` vào bất kỳ nhánh `runtime_memory` nào. Gold chỉ tồn tại trong `eval_only`. (test bắt buộc — xem §4)
- `meta.available_runs`: liệt kê experiment_id/config/stage/prompt_version có trong DB để UI chọn.

**3.2 Route** — blueprint `app/backend/routes/thesis_dataset.py`, `GET /thesis/datasets/<job>` read-only; reuse `common.ok/error`. KHÔNG mutation.

**3.3 Quarantine** — config `THESIS_APP_MODE`: khi `=cockpit`, ẩn/disable route-group AILAB gold-authoring (annotation, normalize, package, references) + nút tương ứng ở frontend; default giữ legacy cho dev. KHÔNG xóa file/route — chỉ gate.

**3.4 Frontend** — `api.js`/`data.jsx` thêm nguồn `thesis`; `parts_right.jsx` render từ `runtime_memory` + `eval_only` ở **2 nhánh tách + badge riêng** (không trộn list glossary); `parts_left.jsx` preview source vs `translations`.

## 4. Acceptance criteria *(offline — 0 API, 0 pipeline run)*

```bash
# 1) adapter shape + GUARD provenance
python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_readmodel.py -v
#   PASS; trong đó BẮT BUỘC 1 test: gold KHÔNG xuất hiện trong runtime_memory.glossary_entries
#   (chỉ trong eval_only.gold_glossary); runtime_memory chỉ từ glossary_entries/entities/entity_relations.

# 2) chạy backend trên DB thật (read-only), 0 API
#   GET /thesis/datasets/treasure_island_p2  → trả runtime_memory + translations S0/S1; eval_only tách
#   GET /thesis/datasets/<d2l_job>           → tương tự (entities rỗng cho D2L là hợp lệ)

# 3) quarantine
#   THESIS_APP_MODE=cockpit → route annotation/package ẩn/404; viewer vẫn render

# 4) smoke frontend
#   load → thấy glossary/entity/relation (nhánh runtime_memory) + preview S0/S1 vs source + badge provenance
```
*(Đường dẫn/endpoint là chỉ định; CodeX khớp layout thực, ghi lệnh thật vào §5.)*

## 5. Implementation notes *(CodeX điền)*

> **Lưu ý:** A01 **0 LLM-call** → 6-mục-LLM của LOCK (ll).6 KHÔNG áp dụng. Thay bằng 5 mục dưới:
- **Data-source policy:** bảng SQLite nào → nhánh read-model nào; đọc read-only (mode=ro).
- **Read-model contract:** shape JSON thật trả về (dán mẫu).
- **Provenance guard:** chứng minh gold/eval-only KHÔNG lẫn vào runtime_memory (test + cách query).
- **Quarantine list:** route/nút AILAB nào bị ẩn, bằng cờ gì, default ra sao.
- **Test plan:** fixture + lệnh + output.
- (kèm) file đổi, quyết định nhỏ + lý do, gotcha.

### 5.1 CodeX implementation notes

**Files changed**
- `THESIS_RUNTIME_TOOL/app/backend/services/thesis_readmodel.py` - new read-only SQLite adapter.
- `THESIS_RUNTIME_TOOL/app/backend/routes/thesis_dataset.py` - new `/api/thesis/datasets` and `/api/thesis/datasets/<job_id>` endpoints.
- `THESIS_RUNTIME_TOOL/app/backend/config.py`, `routes/__init__.py`, `routes/projects.py` - `THESIS_JOBS_ROOT`, `THESIS_APP_MODE`, blueprint registration, normalize quarantine.
- `THESIS_RUNTIME_TOOL/app/prototype/api.js`, `app.jsx`, `parts_center.jsx`, `parts_right.jsx`, `styles.css` - thesis source wiring, read-only guard, translation compare, provenance/eval-only UI.
- `THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_readmodel.py` - offline fixture tests for adapter, route, and quarantine.

**Data-source policy**
- SQLite is opened with URI `mode=ro` in `services.thesis_readmodel._connect_readonly`.
- `documents`, `blocks` -> `document`, `chapters`, `blocks`.
- `glossary_entries` -> `runtime_memory.glossary_entries`.
- `entities` -> `runtime_memory.entities`.
- `entity_relations` -> `runtime_memory.entity_relations`.
- `translation_runs` -> `translations.{config}` and `blocks[].translations.{config}`; `stage` is preserved inside each row.
- `eval_glossary_gold` -> `eval_only.gold_glossary`.
- `reference_eval_only` -> `eval_only.references`.

**Deviation**
- Translations are keyed by `config` (`S0`, `S1`) rather than `stage`, because the UI comparison needs S0/S1 arms. The original `stage`, `experiment_id`, `prompt_version`, `model`, `seed`, and `window_id` remain in each translation row and in `meta.available_runs`.

**Read-model contract**

```json
{
  "meta": {
    "source": "thesis_sqlite_readmodel",
    "job_id": "d2l_p1",
    "read_only": true,
    "available_runs": [],
    "counts": {},
    "provenance": {
      "runtime_memory": "agent-built from pipeline SQLite tables",
      "eval_only": "gold/reference eval-only; never injectable",
      "translations": "translation_runs rows keyed by config"
    }
  },
  "runtime_memory": {
    "glossary_entries": [],
    "entities": [],
    "entity_relations": []
  },
  "eval_only": {
    "gold_glossary": [],
    "references": []
  },
  "translations": {
    "S0": [],
    "S1": []
  }
}
```

Real DB smoke:
- `GET /api/thesis/datasets/treasure_island_p2`: `blocks=1476`, `chapters=40`, `runtime_glossary=22`, `runtime_entities=10`, `runtime_relations=10`, `translation_rows=162`, translations `S0/S1`.
- `GET /api/thesis/datasets/d2l_p1`: `blocks=8803`, `chapters=22`, `runtime_glossary=1608`, `eval_gold_glossary=458`, `translation_rows=2604`, translations `S0/S1`.

**Provenance guard**
- The adapter never queries gold tables when building `runtime_memory`; gold/reference tables are only read into `eval_only`.
- Runtime rows carry `provenance.branch = "runtime_memory"`.
- Eval-only rows carry `provenance.branch = "eval_only"` and `injectable = false`.
- Translation rows carry `provenance.branch = "translations"`.
- Test fixture intentionally uses the same source term in both branches: runtime `agent -> tác nhân`, eval-only gold `agent -> tác tử`. The test asserts the gold target exists only in `eval_only.gold_glossary`, not in `runtime_memory.glossary_entries`.

**Quarantine**
- Default mode remains `legacy`.
- When `THESIS_APP_MODE=cockpit`, `annotation`, `package`, `references` blueprints are not registered.
- Normalize endpoints inside `routes/projects.py` return `404 legacy_feature_quarantined`.
- Thesis viewer remains available.
- Frontend read-only guard disables edit/review/freeze/validate/package-write paths for `thesis:<job_id>` entries.
- QC export remains local/read-only.
- `eval_only` is rendered in a separate right-panel tab and is not merged into glossary.

**Verification**

```bash
python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL\app\backend\tests\test_thesis_readmodel.py -v
```

```text
THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_readmodel.py::test_readmodel_keeps_runtime_memory_and_gold_eval_only_separate PASSED
THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_readmodel.py::test_readmodel_translations_are_keyed_by_config_and_attached_to_blocks PASSED
THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_readmodel.py::test_routes_load_fixture_and_quarantine_gold_authoring PASSED
3 passed in 0.83s
```

Additional offline smokes:
- Full backend suite: `python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL\app\backend\tests -q` -> `91 passed in 25.17s`.
- Flask test client on real DBs: `/api/thesis/datasets`, `/api/thesis/datasets/treasure_island_p2`, `/api/thesis/datasets/d2l_p1` all returned `200`.
- Babel standalone transpile check passed for `parts_project.jsx`, `parts_center.jsx`, `parts_right.jsx`, `app.jsx`.
- Playwright UI smoke with backend `THESIS_APP_MODE=cockpit` on port 5055 and static UI on 5056: loaded `thesis:d2l_p1`; saw provenance banner, `Eval-only` tab with 458 rows, and S0/S1 translation compare after selecting `d2l_introduction_index_b001`.

**Gotchas**
- D2L first loaded block is in Preface and has no translation rows, so S0/S1 comparison appears after selecting a translated chapter/block such as `d2l_introduction_index_b001`.
- The top chrome still says AILAB Dataset Tool; APP-A01 intentionally reuses viewer chrome. Product naming/polish belongs to a later app phase.
- APP-A01 does not expose prompt/cache/token/cost. That is APP-B01 by LOCK (nn).

## 6. Review *(Claude điền)*

- **Verdict: PASS** (Claude, 2026-06-15 — tái kiểm ĐỘC LẬP từ source + test + chạy adapter trên DB THẬT).

**Đã xác minh:**
1. **Scope giữ đúng:** không API, CodeX KHÔNG commit, **CHỈ `app/` bị đụng** — 0 file `pipeline/`/engine/schema thay đổi (git status xác nhận).
2. **Adapter read-only THẬT:** `_connect_readonly` mở `file:...?mode=ro` (uri=True); `meta.read_only=True`. Không thể ghi DB pipeline.
3. **Provenance = guard CẤP-QUERY by-source (không chỉ badge):** `runtime_memory` dựng CHỈ từ `glossary_entries`/`entities`/`entity_relations` (hàm + bảng RIÊNG); `eval_only` CHỈ từ `eval_glossary_gold`/`reference_eval_only`. Test `test_readmodel_keeps_runtime_memory_and_gold_eval_only_separate` dùng fixture **cùng source_term `agent` khác target** (agent `tác nhân` vs gold `tác tử`) rồi assert gold-target KHÔNG vào runtime (line 225) + gold `injectable=False`. **Trên DB THẬT `d2l_p1`** (Claude chạy adapter): runtime items sourced-from-gold = **0**; runtime sources = chỉ `glossary_entries`, label chỉ `agent-built`; gold injectable toàn `False`; **counts khớp bảng CHÍNH XÁC** (runtime 1608 = bảng glossary_entries 1608; gold 458 = eval_glossary_gold 458) → không nhiễm chéo. **Directional-Lock GIỮ.**
4. **PHÂN BIỆT QUAN TRỌNG (Claude suýt mis-flag, đã đính chính):** 108 gold-target trùng giá trị với agent-target = **AGREEMENT** (agent tự sinh đúng dạng tiếng Việt của gold, ~khớp D2L agreement 0.74) — **KHÔNG phải leak**. Guard đúng là **by-source** (=0 vi phạm), không phải trùng-chuỗi. → Ghi chú cho `APP_D01`: view drift/agreement phải trình AGREEMENT (trùng chuỗi agent==gold = TỐT) tách bạch với provenance (luôn riêng) — đừng vẽ agreement thành nhiễm.
5. **Quarantine:** `THESIS_APP_MODE` mặc định `legacy` (an toàn dev, không vỡ AILAB); `cockpit` → 404 `legacy_feature_quarantined` cho gold-authoring (`cockpit_quarantined`). Test `test_routes_load_fixture_and_quarantine_gold_authoring` xác nhận annotation/normalize bị chặn + endpoint thesis vẫn trả runtime/eval tách. Không xóa file/route.
6. **Deviation (CodeX ghi rõ, Claude CHẤP NHẬN):** translations key theo `config` (S0/S1) thay vì chỉ `stage` — UI cần so S0/S1 trực tiếp; metadata vẫn giữ stage/experiment_id/model/prompt_version. Hợp lý.
7. **Test Claude tự chạy lại:** full backend **91 passed**; 3 read-model test pass (gồm leak-guard). `PermissionError D:\temp` = atexit cleanup Windows, exit 0.

**Ghi chú nhỏ (KHÔNG chặn):**
- `treasure_island_p2` hiện là run TI **CŨ** (pre-v3, HYG-02 chưa re-baseline) → viewer A01 sẽ hiện dữ liệu TI cũ tới khi re-baseline. Đúng bản chất A01 (viewer read-only của dữ liệu đã có).
- `d2l_p1` entities/relations=0 (D2L profile để trống) — hợp lệ, không phải lỗi adapter.

**Follow-up:** `APP_B01` (observability cockpit) là task kế. GATE: A01 read-only — KHÔNG mở write/observability/run-control trong phạm vi này (đã giữ đúng).

---

**GATE (LOCK nn):** `APP_A01` READ-ONLY. KHÔNG mở write / observability / run-control / live-stream trong task này. B01 (prompt/cache/cost), D01 (score/drift), C01 (run control) là task sau, theo thứ tự A→B→D→C.
