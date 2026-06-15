# TASK_RUN_EVENT_01_sidecar_event_log — Structured sidecar event log (live per-window observe, engine emit ⊥ compute)

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (oo) [thang leo A→D→B/C; D = sidecar JSONL; 3 guard; KHÔNG token-stream; KHÔNG per-window DB commit] | (nn).1 observe⊥compute, (nn).3 C01 tail-log, (nn).8 run agent BẤT BIẾN | (ll) cost-as-GATE | APP-C01 (RunControl spawn + run_id + log-tail; mẫu endpoint)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

LOCK (oo) chốt 3 bên: live-view giàu hơn = **hướng D = structured sidecar event log**, làm SAU A (APP-C01, DONE). Mong muốn của user ("xem từng block/window hiện ra live") đạt được mà **KHÔNG phá deterministic engine**: engine **append JSONL event mỗi window** ra một FILE riêng (`run_events/<attempt_id>.jsonl`), UI tail file đó; **DB chính vẫn commit MỘT lần cuối** (runner.py:238). Triết lý: **THÊM quan sát mà KHÔNG đổi tính toán** → output/determinism nguyên vẹn, scorer/`translation_runs` sạch; crash thì event log chỉ là artifact debug, scorer KHÔNG BAO GIỜ chấm nhầm.

**Đây là task CHẠM ENGINE đầu tiên của track App** (khác A01–C01 observe-only). Nửa **ENGINE EMIT** là phần review-gắt-nhất: **determinism gate** (bật/tắt emit → output BYTE-IDENTICAL). Nửa **APP CONSUMER** là observe-only như C01.

## 2. Scope

- **IN — (A) ENGINE EMIT (pipeline, off-by-default):**
  - Module MỚI `pipeline/translate/run_events.py`: `EventSink` ghi JSONL append, **best-effort** (tự nuốt lỗi I/O — lỗi sink TUYỆT ĐỐI không được làm crash run) + một `NullEventSink` no-op mặc định.
  - `translate_windows` (runner.py:72) thêm **1 param optional** `event_sink: EventSink | None = None`. `None` ⇒ KHÔNG emit ⇒ **byte-identical với hôm nay**. Emit tại các bước ĐÃ CÓ trong vòng lặp (không thêm tính toán mới): `window_started` · `window_skipped` (nhánh resume) · `prompt_built` (token-estimate + context included/excluded/dropped_by_budget — đồng nhất dữ liệu B01 Inspector) · `request_sent` · `response_received` (cache_key/from_cache/usage/cost) · `json_parsed` (status translated/failed) · `window_preview_available` (bản dịch block CHƯA-COMMIT, **bắt buộc nhãn `committed:false`**) · `persist_buffered` (đã `_persist_*` nhưng CHƯA commit) · cuối: `run_committed` (sau db.commit():238) hoặc `run_failed`.
  - CLI `run_translate.py` (và `run_prepass.py` nếu áp được) thêm cờ `--event-log <path>` (+ `--run-id/--attempt-id`); có cờ ⇒ dựng `EventSink` → truyền vào `translate_windows`; KHÔNG cờ ⇒ `None`. File: `data/jobs/<job>/run_events/<attempt_id>.jsonl`; `attempt_id` lấy từ `--run-id` (C01 truyền run_id của nó) hoặc uuid mới. **`attempt_id` CHỈ nằm ở tên file** — KHÔNG đổi schema DB, KHÔNG đưa vào khóa `run_id` (guard (oo).4.ii).
- **IN — (B) APP CONSUMER (cockpit, observe-only):**
  - `GET /api/thesis/runs/<run_id>/events?offset=` — tail + parse JSONL (gương `read_log` của C01), trả `events[]` + `offset` mới + `running`.
  - C01 RunControl khi spawn `run_translate` truyền sẵn `--event-log data/jobs/<job>/run_events/<run_id>.jsonl` để cockpit biết đường tail.
  - UI panel live tối thiểu: window/block đang chạy · prompt/context tóm tắt · cache hit · token/cost · **preview chưa-commit có nhãn "uncommitted"**.
- **OUT:**
  - **KHÔNG đổi output/determinism.** Emit là side-effect thuần, off-by-default; bật/tắt phải cho `translation_runs` + `TranslateReport` BYTE-IDENTICAL. Không đụng seed/cache/RNG/control-flow.
  - **KHÔNG per-window DB commit** (vẫn commit cuối) — đó là B/C, task khác + cần bảng `run_attempts`.
  - **KHÔNG để sidecar/preview rò vào `translation_runs` / read-model / report** (guard provenance phiên bản D). Sau commit, cockpit đọc DB là nguồn-sự-thật-duy-nhất; sidecar live-only/ephemeral.
  - **KHÔNG token-streaming** (event theo window/request, không theo từng token model sinh).
  - KHÔNG đổi schema DB / thêm `run_attempts` (để dành B/C). KHÔNG auth/multi-user/SaaS-polish.

## 3. Đầu mối dữ liệu / điểm nối *(Claude đã kiểm — imple khớp & ghi §5)*

- `translate_windows(db, windows, client, experiment_id, config, context_builder, context_budget_tokens, profile_name)` — `runner.py:72`; vòng lặp + `db.commit()` cuối ở `runner.py:238`; persist `_persist_run`/`_persist_pack` (1 row/block, 1 pack/window).
- Bước emit map đúng code có sẵn: `build_messages` · `_call_with_reask` · `extract_translations` · `_persist_*`.
- **Test 0-API deterministic:** `pipeline/tests/test_translate_runner.py` — `_fake_result()` (LLMResult giả, cost 0) + `_make_doc_db()`; dùng để chạy `translate_windows` 2 lần cho determinism gate.
- C01: `run_id = run_{uuid}` + `spawn_run` (services/thesis_runs.py), `read_log` (mẫu cho `/events`); `run_translate.py:117` gọi `translate_windows`.
- Đường file gợi ý: `data/jobs/<job>/run_events/<attempt_id>.jsonl` (jobs root = `THESIS_JOBS_ROOT`).

## 4. Acceptance *(0 API — fake client)*

1. **DETERMINISM GATE (cứng):** chạy `translate_windows` 2 lần với fake client trên cùng DB fixture — lần 1 `event_sink=None`, lần 2 có sink — assert `translation_runs` rows + `TranslateReport.to_dict()` **BẰNG NHAU TUYỆT ĐỐI**. (Nếu lệch ⇒ FAIL, emit đã chạm compute.)
2. Sink ON → file `run_events/<attempt_id>.jsonl` có đúng **chuỗi event/window theo thứ tự** (window_started → … → persist_buffered) + cuối `run_committed`; `window_preview_available` có `committed:false`.
3. **PROVENANCE GUARD:** `DatasetReadModel`/read-model KHÔNG đọc event file; `translation_runs` KHÔNG chứa preview chưa-commit; event file là artifact MỚI DUY NHẤT (không ghi thêm bảng nào).
4. **Best-effort:** sink với path không ghi được (vd thư mục read-only) → run vẫn chạy xong, KHÔNG raise; lỗi nuốt gọn.
5. Endpoint `GET /api/thesis/runs/<run_id>/events?offset=` tail đúng (offset tăng dần, parse JSONL) — test với event file giả.
6. `python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL/pipeline/tests THESIS_RUNTIME_TOOL/app/backend/tests -q` — xanh, **0 API**. (atexit PermissionError Windows = vô hại, AGENTS.md §4.)
7. Dán output thật vào §5. KHÔNG chạy run-API thật.

## 5. Implementation notes *(imple điền)*

*(điền: EventSink design + emit points · CLI flag wiring · file path/attempt_id policy · endpoint contract · determinism-gate cách test · provenance-guard · best-effort failure · UI tối thiểu · test output thật)*

## 6. Review *(Claude điền)*

- **Verdict:** (trống)
- Findings: …
- Follow-up: …

---

**GATE (LOCK oo):** D = sidecar JSONL, **observe THÊM, compute KHÔNG đổi**. Off-by-default + **determinism byte-identical** là điều kiện sống còn. **KHÔNG** per-window DB commit, **KHÔNG** sidecar rò vào `translation_runs`/read-model/report, **KHÔNG** token-stream, **KHÔNG** đổi schema/`run_attempts` (để dành B/C). Sidecar live-only; sau commit DB là nguồn-sự-thật. Reviewer gắt nhất ở determinism-gate + provenance-guard.
