# TASK_APP_C01_run_control_live — Run control + live LOG-stream + run provenance (trigger-only)

- **Status:** DONE / PASS
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

## 5. Implementation notes *(CodeX rework, 0 API-call trong test)*

### Summary

Reworked the Antigravity implementation against Claude review §6.  The old §5 notes are superseded: pipeline scripts do exist, prompt-preview is no longer a stub, and tests now include one real frozen pipeline script smoke.

**Files changed:**
- `app/backend/config.py` — added `THESIS_TOOL_ROOT` and `THESIS_PYTHON_EXE`.
- `app/backend/services/thesis_runs.py` — rebuilt RunControl service.
- `app/backend/routes/thesis_runs.py` — rewired Flask route to the new RunControl contract.
- `app/backend/tests/test_thesis_runs.py` — replaced fake-only tests with real module launch + prompt-preview tests.
- `app/prototype/api.js`, `app/prototype/app.jsx`, `app/prototype/parts_center.jsx`, `app/prototype/styles.css` — added minimal Cockpit Run Control UI.
- `app/backend/services/thesis_scores.py`, `routes/__init__.py` — kept the D01 hardening and blueprint registration from the prior implementation.

### Blocking fixes

1. **Launch script thật:** `build_argv()` now builds `python -m pipeline.scripts.<script>` and `spawn_run()` runs with `cwd=THESIS_RUNTIME_TOOL`.  No generic `--job` is injected; argv is mapped per script (`run_translate`, `run_prepass`, `snapshot_runs`, etc.) to avoid invalid flags.
2. **Cost gate thật:** `allow_api=true` now requires `job_id` + a one-time confirm-token.  The token is bound to the exact argv digest and expires after 30 minutes.  A token cannot be reused and cannot be applied to a different config/chapter/cache argv.
3. **Prompt-preview thật:** `/api/thesis/runs/prompt-preview` renders a representative translator prompt from current pipeline code (`build_windows` + `plan_anchors` + `build_context_pack` + `build_messages`) and returns real system/user messages plus token estimates.  It does not call any provider.
4. **Nonzero exit status:** subprocess return code `0` -> `done`; nonzero -> `failed`; internal spawn exception -> `error`.
5. **Windows path handling:** backslash is no longer treated as shell-meta because `Popen(argv_list, shell=False)` is used.  Real shell metacharacters such as `;`, `|`, `&`, backticks, quotes, redirects remain rejected.
6. **UI surface:** Cockpit now contains Run Control: prompt preview, API-confirm checkbox, launch, run registry, and offset-based live log tail.

### Cost-gate deviation from original wording

The original spec said default replay/dry can use `--cache` and still be 0-API.  Current `LLMClient` has replay cache, but no cache-only mode: a cache miss would call the provider.  To keep cost-as-GATE honest, APP-C01 is stricter:

- `allow_api=false` for `run_translate`/`run_prepass` appends `--preflight-only`; it cannot create translations or call API.
- `allow_api=false` for API-capable scripts without safe dry-run (`run_judge`, `build_index`) is rejected with `dry_run_not_supported`.
- Full API-capable execution requires prompt-preview + confirm-token.  Cache hits are still reused by the pipeline; cache misses may call the provider only after explicit confirmation.

This is a conscious safety deviation, not an accidental behavior.

### Endpoint behavior

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/thesis/runs` | Builds per-script argv, records provenance, spawns process, tails log. |
| `GET` | `/api/thesis/runs` | Run registry only; no dataset/observability/score payload. |
| `GET` | `/api/thesis/runs/prompt-preview` | `run_translate` only for now; returns full representative prompt + token estimate + confirm token. |
| `GET` | `/api/thesis/runs/<run_id>` | Full provenance snapshot including cwd/argv/status/exit/log path. |
| `GET` | `/api/thesis/runs/<run_id>/log?offset=N` | Offset-based stdout/stderr tail. |

### Verification output

Targeted APP-C01 tests:

```text
$ python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_runs.py -q
.....................                                                    [100%]
21 passed in 9.75s
```

Full backend regression:

```text
$ python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL/app/backend/tests -q
........................................................................ [ 57%]
.....................................................                    [100%]
125 passed in 72.18s (0:01:12)
```

Python compile:

```text
$ python -m py_compile THESIS_RUNTIME_TOOL/app/backend/services/thesis_runs.py THESIS_RUNTIME_TOOL/app/backend/routes/thesis_runs.py
# pass
```

Real frozen script smoke:

```text
$ python -m pipeline.scripts.snapshot_runs --help
# pass; help includes --db and --out
```

Browser smoke (Chrome channel via Playwright, local backend/prototype only):

```text
Cockpit visible: true
Run Control visible: true
Render prompt preview button visible: true
Prompt preview click result: token issued, preview system visible, preview user visible
Console/page errors: []
```

Representative browser preview observed on `treasure_island_p2`:

```text
Prompt preview token issued
895 prompt tokens
windows: 31
max prompt: 1,711
upper total: 296,841
daily cap: 2,400,000
```

The pytest atexit cleanup still emitted the known Windows `PermissionError` for `D:\temp\pytest-of-Snail\pytest-current` in one run.  Tests had already passed; this matches the existing task note about harmless Windows cleanup noise.

### Known gaps

1. Prompt preview currently supports `run_translate`.  `run_prepass` has preflight-only, but a full Builder prompt preview should be a separate extension if C01 must launch API Builder runs from UI.
2. True cache-only execution is not available in `LLMClient`; APP-C01 therefore refuses to represent cache replay as guaranteed 0-API unless the script is in `--preflight-only` mode.
3. No per-block DB live stream was added.  Live is still stdout/stderr log tail, matching LOCK (nn).3.

## 6. Review *(Claude điền)*

- **Verdict:** REWORK. Implementer: Claude Opus 4.6 (Antigravity), lần 1. Bộ khung an toàn + D01-hardening ĐẠT; nhưng 3 phần CỐT LÕI (launch script thật, cost-gate, prompt-preview) bị stub/sai-wiring và §5 over-claim "complete".
- **Đạt (đã re-verify):**
  - Security skeleton: allowlist + reject shell-meta + KHÔNG shell=True + argv-list + validate job_id. Test thật.
  - Lifecycle pending→running→done/error; log-tail offset-based; run-registry (JSONL) có provenance (argv/seed/config/cache).
  - **D01 hardening (3 note) ĐÚNG & SẠCH:** scope_warning cả d2l/ti; TI drift status_source=derived_from_coverage + target_term_kind=entity_id; bỏ code chết. GIỮ NGUYÊN khi rework.
  - Protocol: Status=REVIEW, KHÔNG commit, known-gap khai báo. 23 + 127 test xanh (re-run).
  - §2 OUT tôn trọng: KHÔNG per-block DB-stream, KHÔNG engine change, test 0-API.
- **PHẢI SỬA (blocking — 3 điểm):**
  1. **Không launch được pipeline THẬT.** build_argv dựng [python, <script>.py] (file-invocation). Script thật import `from pipeline.*` tuyệt đối + có `if __name__=="__main__"` ⇒ BẮT BUỘC `python -m pipeline.scripts.<name>` với cwd=repo-root. Hiện không truyền scripts_root/cwd/-m ⇒ ImportError khi gặp script thật; chỉ chạy `python -c` giả. **Known-gap "Pipeline scripts not in repo" SAI** — script CÓ ở pipeline/scripts/ (đã liệt kê §3). Sửa: invoke -m + cwd=repo-root (+PYTHONPATH nếu cần), thêm THESIS_SCRIPTS_*/repo-root vào config.py, và 1 smoke test với script THẬT 0-API (snapshot_runs/score_consistency) chứng minh launch+exit-code thật.
  2. **Cost-gate có lỗ + không gắn chi tiêu thật.** (a) `expected = get_confirm_token(job_id) if job_id else None`: bỏ job_id ⇒ expected=None ⇒ confirm_token bất kỳ chuỗi non-empty đều qua (bypass). (b) allow_api KHÔNG ảnh hưởng argv ⇒ gate gác một cờ no-op; chi tiêu thật do --cache + hit/miss quyết định. Cho bề-mặt-tiêu-tiền-đầu-tiên đây là gate yếu nhất có thể (trái LOCK ll cost-as-GATE). Sửa: bắt buộc job_id khi allow_api; verify token chặt (khớp token đã phát + one-time/hết-hạn); gate phải THỰC SỰ điều khiển replay-vs-API.
  3. **Prompt-preview là STUB ("N/A").** Vi phạm luật #1 của chủ đề (surface prompt thật + token-estimate TRƯỚC khi chạy) + làm confirm-token vô nghĩa. Sửa: nối render_literary_prompts.py / B01 Inspector để trả prompt đại diện THẬT + token-estimate THẬT + cache-plan; token chỉ phát sau khi preview thật dựng được.
- **Lưu ý phụ:** _SHELL_META_RE chặn cả dấu gạch-ngược ⇒ path Windows kiểu data\jobs\x.sqlite3 bị reject (dùng posix path, hoặc tách path khỏi shell-meta validation).
- **Follow-up:** trả task về implementer sửa 3 điểm; GIỮ D01-hardening. Khi pass mới commit cả cụm (C01 + D01-hardening) một lượt.

- **Re-review (lần 2 — CodeX rework) → PASS / ACCEPT.** Mọi blocker xử lý đúng BẢN CHẤT, đã re-verify từ code thật (không tin §5):
  1. **Launch pipeline THẬT ✓** — `argv[:3] == [python, "-m", "pipeline.scripts.<script>"]` (service:495) + route truyền `cwd=THESIS_TOOL_ROOT` (config mới). **Smoke test script THẬT** `test_real_pipeline_module_help_smoke` chạy `-m pipeline.scripts.snapshot_runs --help` với `cwd=TOOL_ROOT` và pass trong bộ 125 → chứng minh `-m`+cwd resolve package thật, không phải assert suông.
  2. **Cost-gate THẬT ✓** — `allow_api=true` bắt buộc `job_id` (đóng bypass), confirm_token tra store + kiểm TTL + **gắn `job/script/argv_digest`** + **one-time pop** (service:266-296). Route có test reject thiếu token / thiếu job_id.
  3. **Prompt-preview THẬT ✓** — `_render_translate_prompt_preview` import code translate thật (`build_messages`/`build_context_pack`/`plan_anchors`/`estimate_prompt_tokens`), render system/user prompt + token-estimate, **0 API**; `preview_kind="real_translate_prompt"`. Token chỉ phát sau preview.
  4. **Exit nonzero → `failed` ✓** (service:417), không còn ghi `done` sai.
- **Deviation chấp nhận (và còn TỐT HƠN spec gốc của mình):** LLMClient chưa có cache-only mode → CodeX KHÔNG giả vờ "cache=0-API chắc chắn". `allow_api=false` ép `--preflight-only` (cờ THẬT, đã kiểm `run_translate.py:76`/`run_prepass.py:55`); `run_judge`/`build_index` (không có dry-flag, chưa preview) bị **từ chối an toàn** thay vì liều gọi API. Đây vá đúng lỗ trong giả định "replay=0-API" của spec mình.
- **Observe⊥compute ✓** — Run Control UI chỉ preview/launch/tail + hiển thị `target_text/output_text`, KHÔNG tính metric.
- **D01-hardening** giữ nguyên & vẫn đúng. Protocol ✓ (REVIEW, KHÔNG commit). Tự re-run: **21 + 125 pass**.
- **Known-gap còn lại (khai báo thật, chấp nhận):** prompt-preview mới hỗ trợ `run_translate`; Builder/Judge/Embedding muốn chạy-API qua cockpit cần task mở rộng riêng (gắn vào `RUN-EVENT-01`/mở rộng sau). Lưu ý phụ shell-meta-chặn-backslash: CodeX dùng posix path nên không vướng; để ngỏ nếu sau cần path Windows-native.
- **Quyết định:** ACCEPT, commit cả cụm (C01 + D01-hardening + Run Control UI). Khép App.

---

**GATE (LOCK nn):** C01 **trigger-only** — spawn script-freeze + tail LOG + refresh read-model. **KHÔNG** recompute, **KHÔNG** engine/scorer/script change, **KHÔNG** per-block DB-stream (engine commit-cuối), **KHÔNG** exec ngoài allowlist, **KHÔNG** run-API thiếu prompt-preview+confirm. Đây là bề mặt app đầu tiên tiêu quota → cost-gate hạng-nhất. Khép App sau C01.
