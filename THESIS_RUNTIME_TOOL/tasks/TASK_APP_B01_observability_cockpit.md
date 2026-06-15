# TASK_APP_B01_observability_cockpit — ObservabilityReadModel + Prompt/Context Inspector + cache/cost cockpit (read-only)

- **Status:** READY
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

## 6. Review *(Claude điền)*

- **Verdict:** (trống)
- Findings: …
- Follow-up: …

---

**GATE (LOCK nn):** B01 READ-ONLY observability. KHÔNG mở write / score-report (D01) / run-control (C01) / engine-instrument trong task này. B01 cung cấp số cho cost-quality GATE của task re-baseline nhưng KHÔNG thực thi gate đó.
