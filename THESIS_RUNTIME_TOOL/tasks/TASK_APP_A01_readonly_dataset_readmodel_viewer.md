# TASK_APP_A01_readonly_dataset_readmodel_viewer — Read-only DatasetReadModel adapter + viewer + quarantine AILAB gold-authoring

- **Status:** READY
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

## 6. Review *(Claude điền)*

- **Verdict:** (trống)
- Findings: …
- Follow-up: …

---

**GATE (LOCK nn):** `APP_A01` READ-ONLY. KHÔNG mở write / observability / run-control / live-stream trong task này. B01 (prompt/cache/cost), D01 (score/drift), C01 (run control) là task sau, theo thứ tự A→B→D→C.
