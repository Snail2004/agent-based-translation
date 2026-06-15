# TASK_APP_C01_run_control_live — Run control + live LOG-stream + run provenance (trigger-only)

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (nn).3 [APP_C01 = trigger script freeze + tail log, UI KHÔNG tự tính], (nn).5 [History→run provenance], (nn).8 [run agent BẤT BIẾN] + guardrail (nn) [run tạo run_id+config+model+seed+prompt_version; prompt dài preview-TRƯỚC-khi-chạy; KHÔNG live-stream trước khi viewer+report ổn] | (ll) prompt-artifact-review + cost-as-GATE | APP-A01/B01/D01 (read-model để refresh sau run)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

`APP_C01` là **màn cuối** khép cockpit. LOCK (nn).3 chốt: C01 = **trigger script đã-freeze + tail LOG**, UI **KHÔNG** tự tính, **KHÔNG** sửa engine. Đây cũng là **bề mặt app ĐẦU TIÊN có thể tiêu API/quota thật** → cost-gate là hạng-nhất, không phải báo cáo phụ.

**Sự thật kỹ thuật Claude đã kiểm (quyết định thiết kế "live"):** `pipeline/translate/runner.py:238` gọi `db.commit()` **MỘT LẦN ở cuối** vòng lặp window (`_persist_run`/`_persist_pack` ghi trong loop nhưng commit cuối). ⇒ Một read-only connection **KHÔNG thấy row nào cho tới khi cả script chạy xong**. Vì vậy **live = tail LOG (stdout/stderr) + granularity theo script/stage**, **KHÔNG** phải stream từng block từ DB. Điều này **khớp đúng** LOCK (nn).3 ("tail log"). Muốn xem từng-block-hiện-ra-live thì phải đổi engine sang per-window commit = **task pipeline RIÊNG + LOCK mới**, KHÔNG nằm trong app task này.

**Mô hình:** App = **launcher + log-tailer + post-run refresh**. Bấm chạy → backend spawn **đúng script headless đã freeze** (subprocess, argv allowlist) → ghi log ra file → UI tail log live → script xong → UI re-fetch A01/B01/D01 (read-model đã có) để hiện dữ liệu mới. App crash KHÔNG kéo pipeline; replay-cache → dựng lại y hệt cho hội đồng 0-API.

## 2. Scope

- **IN:**
  - **`RunControl` service + endpoint RIÊNG** (tách read-model A01/B01/D01):
    - `POST /api/thesis/runs` — spawn 1 script trong **ALLOWLIST** {`run_prepass`,`build_memory`,`build_index`,`run_translate`,`run_judge`,`score_consistency`,`score_run`,`snapshot_runs`} làm subprocess theo-dõi-được. Body chỉ định: script, db/job, chapters, configs (S0/S1), profile, experiment, seed, cache-path. Trả `run_id`.
    - `GET /api/thesis/runs` — list (run_id, script, status, started/ended, exit_code).
    - `GET /api/thesis/runs/<run_id>` — status + metadata (pid, argv, config/seed/model/prompt_version/cache-path đã capture).
    - `GET /api/thesis/runs/<run_id>/log?offset=` — tail stdout+stderr **tăng dần** (offset-based), trả thêm `running`/`exit_code`.
  - **Cost/API gate (cứng — LOCK ll cost-as-GATE + bộ nhớ token-growth-halt):** mặc định mọi run = **replay/dry** (dùng `--cache` sẵn có → 0-API deterministic). Run **gọi API thật** chỉ khi `allow_api=true` **+ confirm-token** lấy từ prompt-preview (dưới). Tôn trọng quota preflight có sẵn (`llm_client._raise_if_over_quota`) — KHÔNG bypass.
  - **Prompt-preview-TRƯỚC-khi-chạy (LOCK ll/nn):** trước run translate gọi-API, trả prompt đại diện + ước lượng token + cache plan (tái dùng đường render `pipeline/scripts/render_literary_prompts.py` / B01 Inspector). Confirm-token phát hành từ preview này; thiếu → từ chối run API.
  - **Run registry = run provenance (LOCK nn.5):** persist nhẹ (bảng/JSON dưới job dir) mỗi run: run_id + script + **argv đầy đủ** + config/seed/model/prompt_version + cache-path/scope + started/ended/exit_code. Đây là "History→run provenance".
  - **Allowlist + validate argv** (KHÔNG `shell=True`, KHÔNG lệnh/flag tùy ý, KHÔNG ký tự shell-meta trong arg). Dùng tiền lệ `services/annotation_flow.py:787` (`subprocess`), nhưng spawn dạng `Popen(argv_list)` để tail log.
  - **Hardening mang từ review APP-D01 §6 (3 note — gộp vào đây theo yêu cầu):**
    1. `thesis_scores`: khi job_id được hỏi ≠ `report.experiment_id`/`project` thật → thêm `meta.scope_warning` (truy-được, không im lặng).
    2. `thesis_scores` TI drift: đánh dấu `status`/`target_term` là nhãn suy-ra (`status_source: "derived_from_coverage"`; `target_term_kind: "entity_id"`) để hội đồng không hiểu nhầm scorer phát ra.
    3. `thesis_scores`: bỏ code chết (`_d2l_per_chapter` nhánh D tính-rồi-bỏ; fallback oracle dư trong `_ti_scores`).
- **OUT:**
  - **KHÔNG per-block live DB streaming.** Engine commit-cuối (runner.py:238) → live chỉ log-tail + stage. Per-block live = đổi engine sang per-window commit = task pipeline RIÊNG + LOCK mới. **KHÔNG làm ở đây.**
  - **KHÔNG** recompute metric / **KHÔNG** sửa scorer / pipeline / engine / script freeze.
  - **KHÔNG** ghi/sửa frozen memory (freeze trigger bảo vệ; run translate chỉ ghi `translation_runs`/`memory_packs` job-dir).
  - **KHÔNG** exec lệnh ngoài allowlist; **KHÔNG** auth/multi-user/SaaS-polish.
  - **KHÔNG** tự chạy run-API-thật trong acceptance (không tiêu quota khi test).

## 3. Đầu mối dữ liệu / endpoint *(Claude đã kiểm — imple khớp layout thật, ghi §5)*

- **Script freeze + arg thật** (`pipeline/scripts/`): `run_translate.py` (`--db --chapters --configs {S0,S1} --profile --experiment --cache <replay.sqlite3> --report`), `run_prepass.py`, `build_memory.py`, `build_index.py`, `run_judge.py`, `score_consistency.py`, `score_run.py`, `snapshot_runs.py`.
- **Replay determinism:** `--cache data/jobs/translate_cache.sqlite3` khớp-chính-xác → 0-API; miss → gọi API (chịu preflight `llm_client.py:291 _raise_if_over_quota`, `:300 _raise_if_over_prompt_cap`).
- **Subprocess precedent:** `app/backend/services/annotation_flow.py:787`.
- **Read-model refresh sau run:** `/api/thesis/datasets/<job>` (A01), `/api/thesis/observability/<job>` (B01), `/api/thesis/scores/<job>` (D01).
- **Frontend:** `app/prototype/*.jsx` (`api.js` + `parts_center.jsx` đã wired endpoint thesis). Blueprint mới đăng ký ở `routes/__init__.py` prefix `/api`, tách `thesis_scores`/`thesis_observability`/`thesis_dataset`.
- **Config:** thêm (nếu cần) `THESIS_SCRIPTS_PYTHON`/run-log dir env-overridable, kiểu `THESIS_REPORTS_ROOT` (config.py).

## 4. Acceptance *(0 API — KHÔNG chạy translate thật)*

1. `python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_runs.py -v` — dùng script GIẢ (vd `python -c "print(...)"` hoặc echo trong allowlist test) để test vòng đời: tạo run_id → status running→done → log tail tăng dần → exit_code capture → run-registry persist đủ field provenance (argv/seed/config/cache).
2. **Guard tests:** (a) script ngoài allowlist → 400; (b) arg chứa shell-meta/`;`/`|`/`&&` → 400; (c) run `allow_api=true` thiếu confirm-token → từ chối; replay/dry → cho phép; (d) `/runs` ⊥ read-model (không trả `blocks`/`calls`/`headline`/`drift`).
3. **D01 hardening tests:** job_id≠experiment_id → `meta.scope_warning`; TI drift có `status_source`/`target_term_kind`; `test_thesis_scores.py` vẫn xanh sau khi bỏ code chết.
4. `python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL/app/backend/tests -q` — full regression xanh (atexit PermissionError = lỗi Windows vô hại, AGENTS.md §4).
5. Dán output thật vào §5. **KHÔNG** spawn run-API-thật.

## 5. Implementation notes *(imple điền — A01/D01-style, 0 LLM-call trong test)*

*(điền: data-source/allowlist policy · contract endpoint · cost-gate & confirm-token flow · run-registry shape · separation guard · D01-hardening diff · test plan + output thật)*

## 6. Review *(Claude điền)*

- **Verdict:** (trống)
- Findings: …
- Follow-up: …

---

**GATE (LOCK nn):** C01 **trigger-only** — spawn script-freeze + tail LOG + refresh read-model. **KHÔNG** recompute, **KHÔNG** engine/scorer/script change, **KHÔNG** per-block DB-stream (engine commit-cuối), **KHÔNG** exec ngoài allowlist, **KHÔNG** run-API thiếu prompt-preview+confirm. Đây là bề mặt app đầu tiên tiêu quota → cost-gate hạng-nhất. Khép App sau C01.
