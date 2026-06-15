# TASK_APP_B01_observability_cockpit — ObservabilityReadModel + Prompt/Context Inspector + cache/cost cockpit (read-only)

- **Status:** DONE
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (nn).3 [B01 scope + 2-read-model], (ll) prompt=artifact + 6-mục + cost-as-GATE, (gg) token-discipline | APP-A01 (mẫu read-only adapter)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

LOCK (nn).3 chốt `APP_B01` = **Observability cockpit** với **Prompt/Context Inspector BẮT BUỘC** + API calls + cache + token + cost; là **`ObservabilityReadModel` RIÊNG endpoint** (KHÔNG nhét vào schema dataset 1.5.0 của A01). Đây là màn biến LOCK (ll) "prompt = thiết kế memory = lõi luận văn" thành **nhìn-được**, và là **trục Cost-reproducibility** (LOCK kk).

**Nền dữ liệu ĐÃ CÓ (Claude kiểm schema thật — KHÔNG cần thêm logging vào engine):**
- `llm_call_cache`: `request_json` (canonical: model+messages+temp+seed+response_format), `usage_json` (prompt/cached/completion tokens), `cost_usd`, `latency_ms`, `model`, `tag`, `system_fingerprint`.
- `memory_packs`: `payload_json` (pack đã inject), `memory_refs_json`, `retrieval_debug_json` (included/excluded/dropped), `estimated_tokens`, `prompt_version`, `pack_hash`.
- `translation_runs`: `run_id`, `experiment_id`, `config`, `stage`, `prompt_version`, `seed`, `model`, `temperature`, `cost`, `latency_ms`, `pack_id` (FK→memory_packs).
- `usage_daily`: `total_tokens`, `calls`, `cost_usd`. `judge_call_cache`: `usage_json`, `cost_usd`.

→ B01 = **đọc + render**. 0 API, 0 pipeline change, 0 engine logging mới.

## 2. Scope

- **IN:**
  - **`ObservabilityReadModel` adapter** (read-only `mode=ro`), endpoint RIÊNG (vd `GET /api/thesis/observability/<job>` + sub-resource `/calls`, `/calls/<id>`), TÁCH khỏi `/api/thesis/datasets` của A01.
  - **API-calls list:** mỗi call — tag, agent (Builder/Translator/Judge suy từ tag), model, prompt_version, prompt_tokens, cached_tokens, completion_tokens, total, cost, latency, created_at, cache-status.
  - **Prompt/Context Inspector (màn BẮT BUỘC, per-call):** system message + user message (parse từ `request_json.messages`); **memory pack đã inject** (`memory_packs.payload_json`) + `memory_refs_json` + **retrieval_debug** (included/excluded/dropped) — nối qua `translation_runs.pack_id`→`memory_packs`; usage breakdown (prompt/cached/completion); prompt_version/model/seed; cache (replay + provider `cached_tokens`).
  - **Cache/Cost cockpit:** `usage_daily` theo thời gian (tokens/calls/cost); **phân biệt rõ 3 lớp** — local replay cache vs provider `cached_tokens` vs **total quota tokens (input+output)**; tổng hợp theo config/run.
  - **Section-token breakdown** ở mức suy được: context-pack từ `memory_packs.estimated_tokens`; system từ `request_json`; phần còn lại = source/output. (Khớp LOCK (ll) "memory pack chiếm % prompt".)
- **OUT:**
  - Score/report + Consistency/Drift = `APP_D01`.
  - Run control / live-stream = `APP_C01`.
  - **KHÔNG** thêm logging/instrument vào engine. Nếu một tín hiệu THIẾU (vd replay hit/miss COUNT per-run không có sẵn — xem §3.4), B01 **hiển thị cái đang có + ghi rõ khoảng trống**, KHÔNG vá engine (đó là task riêng).
  - KHÔNG write; KHÔNG đụng DatasetReadModel/schema dataset; KHÔNG cost-quality GATE (gate đó ở task re-baseline; B01 chỉ CUNG CẤP số cho nó).

## 3. Spec *(Claude viết)*

**3.1 Adapter** `app/backend/services/thesis_observability.py` (read-only). Mapping:
- **calls** = `llm_call_cache` (+ `judge_call_cache`): parse `usage_json`→{prompt_tokens,cached_tokens,completion_tokens}; `cost_usd`; `tag`→agent. Total quota token = prompt_tokens + completion_tokens (cached tính ĐỦ cho quota, KHÔNG suy từ $ — LOCK gg).
- **call detail** = parse `request_json.messages` → system/user; nối pack qua `translation_runs.pack_id` (hoặc block_id) → `memory_packs.{payload_json, memory_refs_json, retrieval_debug_json, estimated_tokens}`.
- **cost/usage** = `usage_daily` rows + aggregate per config từ `translation_runs.cost`.

**3.2 Route** blueprint `app/backend/routes/thesis_observability.py`, GET read-only; reuse `common.ok/error`; gate dưới `THESIS_APP_MODE` như A01 nếu cần.

**3.3 Frontend** màn mới (parts_*): list calls + panel inspector (system/user/pack/excluded/tokens/cost/cache) + cockpit cache/cost. KHÔNG tự tính metric.

**3.4 Khoảng trống đã biết (ghi rõ, KHÔNG tự vá):** `llm_call_cache` lưu KẾT QUẢ call (mỗi row = 1 call đã chạy), không log từng sự kiện "replay-hit". → B01 hiện được "call này cached/re-playable" + provider `cached_tokens`, nhưng **replay-hit-RATE per run** có thể cần run-event-log (nếu chưa có → ghi là follow-up instrument, không thuộc B01).

## 4. Acceptance criteria *(offline — 0 API)*

```bash
# 1) ObservabilityReadModel: fixture llm_call_cache/memory_packs/translation_runs
python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_observability.py -v   # PASS
#   - calls list có prompt/cached/completion tokens + cost
#   - call detail parse được system/user từ request_json + nối memory_pack (payload + excluded/dropped)
#   - GUARD: endpoint observability TÁCH khỏi /datasets (không trộn shape)
#   - read_only=True

# 2) chạy trên DB THẬT (read-only, 0 API)
#   GET /api/thesis/observability/d2l_p1/calls        → list call thật + token/cost
#   GET /api/thesis/observability/d2l_p1/calls/<id>   → system+user message + pack inject + token breakdown

# 3) regression: A01 + backend cũ không vỡ
python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL/app/backend/tests -q   # PASS
```
*(Đường dẫn/endpoint chỉ định; CodeX khớp layout thật, ghi vào §5.)*

## 5. Implementation notes *(CodeX điền — A01-style, 0 LLM-call nên KHÔNG áp 6-mục-LLM)*

- **Data-source policy:** bảng nào→phần read-model nào; read-only mode=ro.
- **Read-model contract:** shape calls-list + call-detail (dán mẫu JSON thật).
- **Cache/cost semantics:** cách phân biệt replay vs provider cached vs total quota token; nguồn mỗi con số.
- **Known-gap note:** tín hiệu nào thiếu (replay-hit-rate…) + đề xuất follow-up, KHÔNG tự vá engine.
- **Separation guard:** chứng minh observability KHÔNG trộn vào DatasetReadModel.
- **Test plan:** fixture + lệnh + output. (kèm file đổi, gotcha.)

### 5.1 Files changed

- Backend read-model:
  - `app/backend/services/thesis_observability.py`
  - `app/backend/routes/thesis_observability.py`
  - `app/backend/routes/__init__.py`
  - `app/backend/tests/test_thesis_observability.py`
- Prototype UI:
  - `app/prototype/api.js`
  - `app/prototype/app.jsx`
  - `app/prototype/parts_center.jsx`
  - `app/prototype/styles.css`

No pipeline/engine file was changed. No API/model call was made.

### 5.2 Data-source policy

All SQLite reads use read-only URI connections (`mode=ro`). Actual schema layout differs from the remembered spec in one important way:

- `data/jobs/<job>/memory.sqlite3`
  - `translation_runs`: links Translator tags/windows/configs to `pack_id`.
  - `memory_packs`: injected pack payload, `retrieval_debug_json`, `estimated_tokens`, `prompt_version`, `pack_hash`.
- Shared cache DBs under `data/jobs/`
  - `prepass_cache*.sqlite3`: Builder `llm_call_cache` and `usage_daily`.
  - `translate_cache*.sqlite3`: Translator `llm_call_cache` and `usage_daily`.
- Job-local judge cache DBs under `data/jobs/<job>/`
  - `judge*.sqlite3`: Judge `judge_call_cache`/`llm_call_cache` and `usage_daily`.

Filter/link policy:

- Translator rows are linked by exact cache `tag == translation_runs.config + "_" + translation_runs.window_id`; this gives a deterministic `translation_runs.pack_id -> memory_packs` bridge.
- Builder rows are filtered by job/doc tag prefixes such as `prepass_<doc_id>` / D2L/TI family tags.
- Judge rows are included from job-local judge DBs.
- Builder/Judge calls do not have a `memory_pack` FK; B01 displays request/usage/cache but does not fake a pack link.

### 5.3 Read-model contract

Endpoints added:

- `GET /api/thesis/observability/<job_id>`
- `GET /api/thesis/observability/<job_id>/calls`
- `GET /api/thesis/observability/<job_id>/calls/<source>:<cache_key>`

Call-detail adds `messages`, linked `memory_pack`, normalized `context_audit`, `linked_runs`, and `token_breakdown`.

`context_audit` surfaces the prompt-context audit without requiring the user to search raw JSON:

```json
{
  "included_count": 17,
  "excluded_count": 0,
  "dropped_by_budget_count": 0,
  "included_sample": ["AI -> tri tue nhan tao", "logarithm -> logarit"],
  "excluded_sample": [],
  "dropped_by_budget_sample": [],
  "anchors_count": {"terms": 17, "entities": 0, "address_policies": 0}
}
```

### 5.4 Cache/cost semantics

B01 separates three notions:

- Local replay cache: a row exists in `llm_call_cache` / `judge_call_cache`, so the request/result is replayable without another provider call.
- Provider cached input: `usage_json.cached_tokens`; this is provider-side cache discount/diagnostic.
- Quota tokens: `prompt_tokens + completion_tokens`. Cached input still counts in the daily token quota, matching LOCK (gg).

Cost is computed from per-call `cost_usd` rows. Actual `usage_daily` tables in the current cache DBs do not consistently expose cost columns, so B01 treats `usage_daily` as supporting time-series data and uses call rows for cost totals.

### 5.5 Known gaps / deviations

- `llm_call_cache` stores executed result rows, not event rows. B01 can show replayable rows and provider `cached_tokens`, but cannot compute replay-hit-rate per run. This remains a follow-up instrumentation task, not part of B01.
- D2L observability includes all matching historical cache rows across shared cache DBs, including old/rework Builder rows. Therefore `d2l_p1` observed token totals are an audit history for the job, not a clean single benchmark-run cost. Run-scoped replay-hit/rerun accounting needs a future run-event log.
- `totals.by_source` was not added because source-level grouping was not required by the UI; the call list still carries `source` per row.

### 5.6 Separation guard

`ObservabilityReadModel` is exposed only under `/api/thesis/observability/...`. It is not mixed into `/api/thesis/datasets/...` and does not alter DatasetReadModel shape from APP-A01. The dedicated route test verifies the two endpoints remain separate.

### 5.7 Verification

Commands run:

```powershell
python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL\app\backend\tests\test_thesis_observability.py -v
# 3 passed in 1.11s

python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL\app\backend\tests -q
# 94 passed in 60.76s
```

The full backend run emitted a Windows temp cleanup warning after success:

```text
PermissionError: [WinError 5] Access is denied: 'D:\temp\pytest-of-Snail\pytest-current'
```

This happened in pytest's atexit cleanup after all tests passed; no test failed.

Real DB smoke (read-only, no API/model call):

- `d2l_p1`: 978 observed cache rows, 4,240,928 observed quota tokens across historical matching cache rows; linked Translator detail opened `pk_S1_w_d2l_multilayer_perceptrons_060` with `included_count=17`, `excluded_count=0`, `dropped_by_budget_count=0`.
- `treasure_island_p2`: 388 observed cache rows, 279,273 observed quota tokens; linked Translator detail opened `pk_S1_w_ch03_019` with prompt messages and context audit.

Playwright UI smoke:

- Opened `http://127.0.0.1:5056/index.html` against backend `http://127.0.0.1:5055/api`.
- Selected thesis job `thesis:d2l_p1` and `Cockpit`.
- Verified API-calls list renders 978 rows.
- Clicked a Translator row and verified the Prompt/Context Inspector renders system prompt, user prompt, memory pack, included/excluded/dropped counts, `anchors_count`, and cache/cost semantics.
- Browser console after reload: 0 errors, 1 non-blocking warning.

## 6. Review *(Claude điền)*

- **Verdict: PASS** (Claude, 2026-06-15 — tái kiểm ĐỘC LẬP từ source + test + chạy adapter trên DB THẬT).

**Đã xác minh:**
1. **Scope:** không API, CodeX KHÔNG commit, **chỉ `app/`** đụng (0 pipeline/engine/schema).
2. **Read-only:** `_connect_readonly` `mode=ro`; `meta.read_only=True` (cả test lẫn DB thật).
3. **Separation guard (2-read-model):** service + route + shape RIÊNG; có **test riêng** `test_observability_routes_are_separate_from_dataset_readmodel` (PASS). ObservabilityReadModel ⊥ DatasetReadModel — không trộn vào schema dataset 1.5.0.
4. **Token/quota semantics ĐÚNG (LOCK gg):** `total_quota_tokens = prompt + completion`, **cached KHÔNG bị trừ** (test 120+20=140; DB thật 2.58M+1.66M=4.24M); `cached_tokens` + `reasoning_tokens` surface RIÊNG. Quota đếm token, không suy từ $.
5. **Cache 3 lớp:** local replay vs provider `cached_tokens` vs total quota — phân biệt rõ.
6. **Prompt/Context Inspector (màn bắt buộc — đạt):** parse `request_json`→system/user; nối `translation_runs.pack_id`→`memory_packs`; surface `included`/`excluded`/`dropped_by_budget` + sample + `context_audit` counts + `token_breakdown` (actual prompt vs estimated pack) — KHÔNG bắt đọc JSON thô.
7. **Multi-DB ĐÚNG:** đọc các file cache RIÊNG (`prepass_cache*`/`translate_cache*` ở jobs_root, `judge*` ở job_dir), KHÔNG phải `memory.sqlite3` per-job. Hiểu đúng layout.
8. **Known-gap ghi TRUNG THỰC trong `meta`:** "replay-hit events are not logged" → chưa tính replay-hit-rate per-run; KHÔNG tự vá engine (đúng spec).
9. **Test Claude tự chạy lại:** 3 observability + 94 backend PASS; DB thật `d2l_p1` 978 call, agents {Builder, Translator}, quota 4.24M / cost $3.77. `PermissionError D:\temp` = atexit Windows, exit 0.

**Ghi chú QUAN TRỌNG cho task RE-BASELINE (cost-quality GATE):** totals hiện là **lịch sử cache TÍCH LŨY** (toàn bộ dev+benchmark, 978 call / 4.24M tok / $3.77 cho D2L), **KHÔNG phải cost sạch của MỘT run**. → Khi dùng B01 cho cost-quality gate, **PHẢI scope theo `run_id`/`experiment_id`/cửa-sổ `created_at`** — đọc raw totals như "cost của run này" = lỗi scope (anh em với scope=scope validity). CodeX đã flag; mình tô đậm vì đây là điểm dễ sai khi báo cáo chi phí.

**Ghi chú nhỏ:** D2L agents chỉ {Builder, Translator} (chưa có Judge call cho D2L) — hợp lệ. TI khi re-baseline xong sẽ có thêm dữ liệu observability.

**Follow-up:** `APP_D01` (score/report + Consistency/Drift) kế. Có thể cần view **cost theo-run** (lọc call theo experiment/run/time) cho gate re-baseline — gắn vào D01 hoặc task re-baseline. GATE: B01 read-only — không mở write/score/run-control/engine-instrument (đã giữ đúng).

---

**GATE (LOCK nn):** B01 READ-ONLY observability. KHÔNG mở write / score-report (D01) / run-control (C01) / engine-instrument trong task này. B01 cung cấp số cho cost-quality GATE của task re-baseline nhưng KHÔNG thực thi gate đó.
