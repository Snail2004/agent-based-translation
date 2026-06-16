# TASK_RUN_EVENT_01_sidecar_event_log — Structured sidecar event log (live per-window observe, engine emit ⊥ compute)

- **Status:** DONE / PASS
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
- **IN — (C) GUARD bổ sung (chốt 3-bên sau review CodeX — acceptance BẮT BUỘC):**
  - **#3 (hazard lớn nhất) — event flags KHÔNG được phá confirm-token C01.** C01 token bind với `argv_digest` chính xác (thesis_runs.py:249). `--event-log`/`--run-id` PHẢI đi qua **cùng `build_argv()`** dùng cho CẢ prompt-preview LẪN create-run ⇒ token bao trùm argv cuối (gồm event flags), và user nhìn đúng command sẽ chạy. KHÔNG để RunControl thêm flag SAU khi preview đã phát token.
  - **#4 — KHÔNG dump full prompt vào event JSONL.** Payload event chỉ chứa **bounded summary/hash/token/context-audit** (included/excluded/dropped counts). Full request đã nằm ở `llm_call_cache`/B01 Inspector. Dump full prompt mỗi window = bloat + log nhạy cảm, trái bài học cost/context.
  - **#7 — preflight-only KHÔNG có event.** `--preflight-only` return trước `translate_windows` (run_translate.py:106, trước cả `_ensure_api_key`/LLMClient) ⇒ UI KHÔNG kỳ vọng events cho preflight run (trừ khi định nghĩa event preflight riêng — ngoài scope).
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
- `TranslateReport.to_json_dict()` (runner.py:52) — KHÔNG có `to_dict`. `--preflight-only` return ở run_translate.py:106 TRƯỚC `translate_windows` (117) ⇒ không emit. C01 token bind argv-digest ở thesis_runs.py:249 (xem guard #3).

## 4. Acceptance *(0 API — fake client)*

1. **DETERMINISM GATE (cứng):** tạo **2 DB clone GIỐNG HỆT** (KHÔNG dùng chung 1 DB — resume/`INSERT OR REPLACE` sẽ che lỗi), chạy fake client: DB-A `event_sink=None`, DB-B có sink — assert **sorted `translation_runs`** (+ `memory_packs` nếu cần) + `report.to_json_dict()` **BẰNG NHAU TUYỆT ĐỐI**. (Lệch ⇒ FAIL, emit đã chạm compute.) [tên method: `to_json_dict()` runner.py:52]
2. Sink ON → file `run_events/<attempt_id>.jsonl` có đúng **chuỗi event/window theo thứ tự** (window_started → … → persist_buffered) + cuối `run_committed`; `window_preview_available` có `committed:false`.
3. **PROVENANCE GUARD:** `DatasetReadModel`/read-model KHÔNG đọc event file; `translation_runs` KHÔNG chứa preview chưa-commit; event file là artifact MỚI DUY NHẤT (không ghi thêm bảng nào).
4. **Best-effort (inject failing sink, KHÔNG dựa read-only dir):** inject một sink mà `write` RAISE → `translate_windows` vẫn chạy xong, KHÔNG raise (lỗi nuốt gọn). (Tránh test thư mục read-only — flaky trên Windows.)
5. Endpoint `GET /api/thesis/runs/<run_id>/events?offset=` — resolve path **TỪ run registry** (`event_log_path` lưu lúc create-run), validate nằm DƯỚI `THESIS_JOBS_ROOT/run_events`, **từ chối path client gửi / path-traversal**; tail offset tăng dần, parse JSONL — test với event file giả.
6. `python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL/pipeline/tests THESIS_RUNTIME_TOOL/app/backend/tests -q` — xanh, **0 API**. (atexit PermissionError Windows = vô hại, AGENTS.md §4.)
7. Dán output thật vào §5. KHÔNG chạy run-API thật.

## 5. Implementation notes *(CodeX, 2026-06-16)*

- Added `pipeline/translate/run_events.py` with `EventSink`, `NullEventSink`, and `emit_event()`. Event rows are JSONL with `schema=run_event_v1`, `seq`, UTC timestamp, `run_id`, and `attempt_id`. Sink errors are swallowed.
- `translate_windows(..., event_sink=None)` is off by default. Emit points: `window_started`, `window_skipped`, `prompt_built`, `request_sent`, `response_received`, `json_parsed`, `window_preview_available`, `persist_buffered`, `run_committed`, `run_failed`. DB commit policy is unchanged: one `db.commit()` at the end of `translate_windows`.
- Event payloads are bounded. Prompt events include hashes/token/message summaries, not full prompt text. Response events include cache key/from_cache/usage/cost. Preview events include block ids plus truncated preview text and `committed:false`.
- CLI: `run_translate.py` accepts `--event-log`, `--run-id`, and `--attempt-id`. `--preflight-only` still returns before `translate_windows`, so preflight emits no events.
- C01 cost gate is preserved. Prompt-preview now issues `planned_run_id` and an event log path. `build_argv()` includes `--event-log/--run-id` for both prompt-preview and create-run, so `validate_api_gate()` still checks the exact final argv digest. Real `run_translate allow_api=true` requires `planned_run_id` from prompt-preview.
- App endpoint: `GET /api/thesis/runs/<run_id>/events?offset=` tails JSONL from the run registry's `event_log_path`, rejects paths outside `THESIS_JOBS_ROOT/run_events`, and never accepts a client-supplied path.
- UI: Cockpit RunControl now polls `/events` alongside stdout log tail and displays latest event, cache/cost/token summary, context summary, recent event list, and uncommitted preview labels.
- Provenance guard: no DB schema changes, no sidecar reads in read-model/report, no preview persisted into `translation_runs`. Sidecar remains live/debug-only.

Test output:

```text
# from THESIS_RUNTIME_TOOL
python -m pytest -p no:cacheprovider pipeline\tests\test_translate_runner.py -q
11 passed in 16.07s

# from repo root
python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL\app\backend\tests\test_thesis_runs.py -q
23 passed in 9.54s

# from THESIS_RUNTIME_TOOL
python -m pytest -p no:cacheprovider pipeline\tests app\backend\tests -q
250 passed in 94.77s (0:01:34)

# from THESIS_RUNTIME_TOOL
python -m py_compile pipeline\translate\run_events.py pipeline\translate\runner.py pipeline\scripts\run_translate.py app\backend\services\thesis_runs.py app\backend\routes\thesis_runs.py
PASS

# from THESIS_RUNTIME_TOOL
python -m pipeline.scripts.run_translate --help | Select-String -Pattern "event-log|run-id|attempt-id|preflight-only"
--preflight-only / --event-log / --run-id / --attempt-id shown
```

Note: pytest still prints the known Windows atexit cleanup `PermissionError` for `D:\temp\pytest-of-Snail\pytest-current`; exit code is 0 and tests pass. No API calls were made.
## 6. Review *(Claude điền)*

- **Verdict:** PASS / ACCEPT. Implementer: CodeX. Mọi guard (oo) + 7 điểm CodeX-review đều implement THẬT và CÓ TEST; re-verify độc lập từ code, không tin §5.
- **Re-verify độc lập:**
  - **Determinism gate ✓ (cứng nhất):** `test_event_sink_on_off_is_compute_identical_on_cloned_dbs` — **2 DB clone riêng**, so `to_json_dict()` + `_stable_translation_rows` + `_stable_pack_rows` bằng nhau tuyệt đối. Mình tự chạy: pass.
  - **Engine additive, không viết lại output-path ✓:** runner +313 = emit-calls + 4 helper bounded-payload (`_messages_hash/_messages_summary/_context_pack_summary/_bounded_translations`); danh sách helper cũ + thứ tự + `db.commit()` cuối (vẫn 1 lần) GIỮ NGUYÊN. 11 test hành vi cũ (resume/reask/partial/multi-window/packs/report) vẫn xanh.
  - **#3 (hazard cross-task) ✓:** preview sinh `planned_run_id` → `event_log_path` → `build_argv(event_log,run_id)` → token bind `argv_digest` GỒM event flags; create-run tái dùng `planned_run_id`. `test_confirm_token_must_match_exact_argv` → mismatch; thiếu token → 403.
  - **#4 không dump prompt ✓:** test assert `"content" not in prompt_built`; payload qua `_bounded_translations(limit=8)` + summary/hash.
  - **#5 endpoint path-from-registry ✓:** `test_route_run_events_tails_registered_sidecar_only` — tail từ `event_log_path` trong registry; path ngoài jobs-root → `invalid_event_log_path` (chặn traversal).
  - **#6 best-effort ✓:** `FailingSink.emit` raise → run vẫn xong, `translation_runs` ghi đủ.
  - **Provenance guard ✓:** KHÔNG read-model nào (dataset/observability/scores) đọc `run_events`; sidecar không là nguồn scorer. `committed:false` trên preview.
  - **Separation ✓:** `test_runs_endpoint_separate_from_readmodels`. D01-hardening tests vẫn xanh.
  - Sink (`run_events.py`): no-op default + nuốt mọi exception + `_json_safe` bounded. Protocol: REVIEW, KHÔNG commit. Tự chạy: **34 targeted + 250 full**, 0 API.
- **Known-gap (chấp nhận):** event chỉ cho `run_translate` (khớp prompt-preview-support); preflight-only KHÔNG có event (#7, đúng thiết kế). Ingest ("load tài liệu") chưa nằm trong allowlist RunControl — xem ghi chú flow ở câu trả lời.
- **Quyết định:** ACCEPT, commit cả cụm (engine emit + RunControl wiring + UI event panel). Hướng D (RUN-EVENT-01) DONE; B/C vẫn chỉ làm khi cần + có `run_attempts`.

---

**GATE (LOCK oo):** D = sidecar JSONL, **observe THÊM, compute KHÔNG đổi**. Off-by-default + **determinism byte-identical** là điều kiện sống còn. **KHÔNG** per-window DB commit, **KHÔNG** sidecar rò vào `translation_runs`/read-model/report, **KHÔNG** token-stream, **KHÔNG** đổi schema/`run_attempts` (để dành B/C). Sidecar live-only; sau commit DB là nguồn-sự-thật. Reviewer gắt nhất ở determinism-gate + provenance-guard.
