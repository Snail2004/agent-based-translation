# TASK_APP_E03_console_results_memory — Console: panel RESULTS + hiển thị MEMORY/pack, khép kín one-button

- **Status:** REVIEWED — Phase A + C **PASS** (verified, committed) · Phase B **REWORK** (renderer merged nhưng trơ, cần emit event → TASK_APP_E04)
- **Refs:** TASK_ONE_BUTTON_V1 (E1/E2 doc-only endpoints) | TASK_EVAL_SCORING_V1 (metric TC/TA/SF-QE/SF-BT/PJ + gate) | TASK_RUN_EVENT_01 (sidecar event schema) | TASK_APP_B01 (Cockpit MemoryPackInspector — nguồn field pack) | TASK_APP_D01 (score report/drift)
- **Branch/Commit:** (điền khi imple xong)
- **Người làm:** CodeX implement · Claude review độc lập

## 1. Bối cảnh & mục tiêu *(Claude viết)*

Agent Console hiện chứng minh pipeline **đã chạy** (event stream, stages, cost luỹ kế, watchlist, latest-block typewriter, pause/replay/cancel). Nhưng nó chưa trả lời 2 câu là **điểm của luận văn**:

1. **KẾT QUẢ**: các stage `score·phase 1 / sf_qe / sf_bt / score·final` chạy xong đều xanh "done" nhưng **không hiện con số nào**. Console kết thúc mà không có "phán quyết" → hội đồng sẽ hỏi ngay. Cần panel RESULTS chốt bằng các metric đã LOCK ở TASK_EVAL_SCORING_V1.
2. **MEMORY**: lõi đề tài là *prompt = memory design*. Console thấy `prompt dựng xong` nhưng không thấy **trong pack có gì** (bơm mấy term, mandatory/soft/excluded, drop vì budget, tokens). Dữ liệu này đã có trong Cockpit (MemoryPackInspector) nhưng chỗ demo (Console) thì trống.

Mục tiêu: khép kín one-button — chạy xong là **thấy điểm + thấy trí nhớ agent** ngay trong Console, tái dùng skin sẵn có, không vẽ CSS mới.

## 2. Scope

- **IN:**
  - **Phase A (ưu tiên, 0-API, đọc artifact đã commit):** endpoint read-only `report-summary` + panel `:: RESULTS` bên cột phải Console.
  - **Phase B (cần thêm field vào event):** một dòng/window trong event feed cho biết pack đã bơm gì; (tùy chọn) mini-panel `:: MEMORY (last window)`.
  - **Phase C (frontend thuần, 0 backend):** duration cho từng stage; nhãn cost "trần vs thực"; tiến độ block đúng cho chương thật (bỏ "/7 win" hardcode).
- **OUT:**
  - KHÔNG vẽ CSS mới, KHÔNG thêm màu/không inline color. Chỉ tái dùng class trong `console.css`.
  - KHÔNG đụng logic scoring/pipeline (chỉ ĐỌC + HIỂN THỊ). KHÔNG ghi vào run_dir/frozen DB.
  - KHÔNG tự bịa metric/threshold không có trong artifact hoặc TASK_EVAL_SCORING_V1.
  - KHÔNG đổi skin Console (màu, dot, layout đã chốt ở các commit parity gần đây).

## 3. Spec *(Claude viết)*

### Guardrail chung (đọc trước khi code)
- **Tái dùng class `console.css`, không tạo class/màu mới.** Các mảnh có sẵn phủ đủ nhu cầu: `.section-label`, `.kv-row/.kv-label/.kv-value` (+ `.kv-good/.kv-warn/.kv-bad/.kv-dim`), `.bar/.bar-fill`, `.banner/.banner-green/.banner-red` (verdict), `.btn/.btn-mini`, `.artifact-path`, `.watch-row`, và feed `.ev-*`. Nếu thấy "thiếu class" → **dừng lại hỏi**, gần như chắc là đã có.
- **Bám đúng đường dây E1/E2 đã có** (block-preview + watchlist) làm khuôn: cùng cách resolve run từ registry, cùng `ok()/error()`, cùng path-guard `_path_under_jobs`, cùng chỗ nối `api.js → app.jsx poll loop → parts_center adapter → console.jsx props`.
- **Wiring thật, không stub.** Mỗi phase phải có 1 smoke chạm target thật trên `run_214651ffe3d5` (không chỉ test biên đã mock). Ref: green-tests-can-hide-dead-integration.

### Phase A — endpoint `report-summary` + panel RESULTS

**A1. Backend** — `THESIS_RUNTIME_TOOL/app/backend/routes/thesis_runs.py` (mirror `run_block_preview`/`run_watchlist` ~dòng 364–400):
- Thêm route `@bp.get("/thesis/runs/<run_id>/report-summary")`.
- Resolve `run_dir` từ registry entry (dùng lại helper resolve run_dir như `_watchlist_path_from_entry`; reports nằm ở `run_dir / "reports"`). Đọc nếu tồn tại: `reports/score_run_phase_1.json`, `reports/score_run_final.json`. Guard bằng `_path_under_jobs`. Read-only, `mode=ro` nếu phải mở sqlite (không nên — đây là JSON file).
- **BẮT BUỘC đọc schema thật trước khi map**: mở artifact thật của `run_214651ffe3d5` (dò run_dir qua registry, ví dụ `data/jobs/<job>/one_button/run_214651ffe3d5/reports/score_run_final.json`) **và** `TASK_EVAL_SCORING_V1.md` để biết tên field/metric CHÍNH XÁC. **Không bịa**: metric nào artifact không có thì bỏ, không fabricate; threshold/gate chỉ set khi LOCK có định nghĩa, còn lại để `status: null`.
- **Response contract (chuẩn hóa — đây là interface frontend bám, giữ ổn định dù schema raw đổi):**
  ```json
  {
    "phase_1": {
      "present": true,
      "metrics": [
        {"key":"TC","label":"term consistency","value":0.93,"unit":"ratio","status":"good"},
        {"key":"TA","label":"term adherence","value":0.88,"unit":"ratio","status":"warn"}
      ],
      "configs": null
    },
    "final": {
      "present": true,
      "metrics": [ {"key":"SF_QE","label":"CometKiwi","value":0.71,"unit":"score","status":null}, ... ],
      "verdict": {"pass": true, "reasons": []},
      "report_path": "reports/score_run_final.json"
    },
    "compare": {"present": false, "gap": null}
  }
  ```
  - `status ∈ {good,warn,bad,null}` (null = trung tính, không tô). `compare` chỉ present khi có cả S0 & S1 (khi đó thêm per-config + gap; nếu run chỉ 1 config thì `present:false`).
  - Thiếu report (chưa score xong) → `phase_1/final.present:false`, HTTP 200 (KHÔNG 500), giống watchlist trả `[]`.

**A2. Frontend nối dữ liệu** (mirror y hệt watchlist):
- `api.js`: thêm `getThesisRunReportSummary: (runId) => request('/thesis/runs/' + runId + '/report-summary')` (cạnh `getThesisRunWatchlist`).
- `app.jsx`: trong đúng poll loop đang fetch song song blockPreview + watchlist (chỗ set `runBlockPreview`/`runWatchlist`, và prop tại ~dòng 2477), thêm fetch `reportSummary` song song `.catch(()=>null)`, lưu state `runReportSummary`, truyền `runControl.reportSummary`.
- `parts_center.jsx` — adapter `AgentConsole` (~dòng 566): truyền `reportSummary={runControl.reportSummary || sel.reportSummary || null}` vào `<AgentConsoleView>`.

**A3. Frontend render** — `console.jsx` `AgentConsoleView`:
- Destructure `reportSummary = null` ở block props (~dòng 177).
- Chèn section MỚI trong `col-right`, **ngay dưới `:: latest artifact`** (dòng 382–383), **trên `:: watchlist`** (dòng 385). Chỉ dùng class có sẵn:
  ```jsx
  <div className="section-label">:: results</div>
  {reportSummary && (reportSummary.final?.present || reportSummary.phase_1?.present) ? (
    <>
      {(reportSummary.final?.metrics || reportSummary.phase_1?.metrics || []).map(m => (
        <div className="kv-row" key={m.key}>
          <span className="kv-label">{m.label || m.key}</span>
          <span className={"kv-value " + (m.status === "good" ? "kv-good" : m.status === "warn" ? "kv-warn" : m.status === "bad" ? "kv-bad" : "")}>
            {m.value == null ? "—" : m.value}
          </span>
        </div>
      ))}
      {reportSummary.final?.present && reportSummary.final.verdict && (
        <div className={"banner " + (reportSummary.final.verdict.pass === false ? "banner-red" : "banner-green")}>
          <span className="banner-glyph">{reportSummary.final.verdict.pass === false ? "✕" : "●"}</span>
          <span className="banner-msg">{reportSummary.final.verdict.pass === false ? ("Gate FAIL · " + (reportSummary.final.verdict.reasons||[]).join(", ")) : "Gate PASS"}</span>
        </div>
      )}
      {reportSummary.final?.report_path && <div className="artifact-path">{reportSummary.final.report_path}</div>}
    </>
  ) : <div className="artifact-path kv-dim">Chưa có điểm — hiện sau khi score chạy xong.</div>}
  ```
  (CodeX được tinh chỉnh format số/nhãn, nhưng KHÔNG thêm class/màu ngoài danh sách trên.)
- `deriveConsoleState` KHÔNG đổi (report đến từ prop như blockPreview/watchlist).
- Dev harness `console_dev.html` không truyền `reportSummary` → phải hiện empty state gọn, không vỡ.

### Phase B — MEMORY/pack theo window (cần thêm field event)

**Phụ thuộc:** cần đưa số liệu pack vào event. Nguồn field đã có ở `memory_packs.context_audit` (Cockpit đọc: `included_count / excluded_count / dropped_by_budget_count / anchors_count`, và `estimated_tokens`).

**B1. Backend/pipeline** — nơi Translator emit event window (TASK_RUN_EVENT_01 sidecar) và nơi pack được build cho window đó: đính kèm vào payload của event window sẵn có (`window_preview_available` hoặc `prompt_built`) **hoặc** emit event mới `pack_built`:
```json
"pack_summary": {"injected": 12, "mandatory": 4, "soft": 6, "excluded": 2, "dropped_by_budget": 1, "est_tokens": 380}
```
Map từ context_audit (injected = included_count; mandatory/soft = phân tách theo injection_action nếu có, ref pack-exclusion-injection-action-gate; nếu chưa tách được thì để mandatory/soft = null, chỉ đưa injected/excluded/dropped/est).

**B2. Frontend** — `console.jsx` `consoleMessageFor()` (dòng 48–84): thêm/mở rộng case tương ứng render 1 dòng, **0 CSS mới** (dùng lại `.ev-row`):
```
w{win} · pack {injected} inj ({mandatory} mand/{soft} soft/{excluded} excl) · {dropped} drop · {est}tok
```
- (Tùy chọn) mini-panel `:: memory (last window)` ở col-right bằng `.kv-row` — cùng class. Giữ optional, không bắt buộc.

**B3. Cập nhật fixture/test (bắt buộc nếu đổi event):** nếu thêm field/emit event mới → cập nhật golden `fixtures/one_button_preface_golden/events.jsonl`, test event-schema, và normalizer của `console_dev.html`, kẻo dev harness/test lệch. Thêm smoke chạy 1 window thật khẳng định `pack_summary` xuất hiện (green-tests-can-hide-dead-integration).

### Phase C — polish (frontend thuần, 0 backend, 0 CSS mới)
- **Duration/stage:** `deriveConsoleState` đã có `stageInfo[*].start/end` (ts). Tính `dur = end - start`, đổ vào `.stage-eta` (đang rỗng, console.jsx dòng 376) dạng `2.3s` / `1m04`. Chỉ frontend.
- **Cost trần vs thực:** dòng cost hiện là **trần trên/ước tính** (one-button-cost-cap-semantics: cascade T3 Gemma ~$0 → over-state). Sửa NHÃN cột COST cho rõ là "cap (upper-bound)"; nếu report có cost thực thì thêm dòng "actual". Chỉ wording.
- **Tiến độ block chương thật:** label translator active đang hardcode `"/7 win"` (console.jsx dòng 363). Thay `7` bằng tổng window/block thật (từ payload event tổng, hoặc `blockPreview.length`), tránh sai trên chương ≠ preface.

## 4. Acceptance criteria *(lệnh chạy được)*

```bash
# --- Phase A ---
# 1) endpoint trả metric + verdict trên run đã có artifact:
curl -s http://localhost:5000/thesis/runs/run_214651ffe3d5/report-summary | python -m json.tool
#    → final.present=true, ≥1 metric, verdict{...}, report_path="reports/score_run_final.json"
# 2) run KHÔNG có report → present:false, HTTP 200 (không 500)
# 3) pytest route (mirror test thesis_runs sẵn có): shape đúng + missing-report -> present:false
python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests -k report_summary -v   # → PASS
# 4) Frontend: Console trên run_214651ffe3d5 hiện ":: RESULTS" (metric + verdict), 0 lỗi console;
#    console_dev.html vẫn render (empty state RESULTS, không vỡ).

# --- Phase B ---
# 5) replay/chạy 1 window translator → event có pack_summary; feed Console hiện dòng "w{n} · pack ...";
python -m pytest THESIS_RUNTIME_TOOL -k "event and pack" -v   # → PASS; golden fixture updated & green

# --- Phase C ---
# 6) stage hiện duration; chương ≠ preface hiện đúng X/N (không còn "/7").
```

## 5. Implementation notes *(CodeX — tóm tắt từ báo cáo)*

- Endpoint read-only `GET /api/thesis/runs/<id>/report-summary` (thesis_runs.py) đọc score_run_phase_1/final.json, không bịa metric/threshold (status=null).
- Nối api.js → app.jsx (poll song song) → parts_center.jsx → console.jsx. Panel `:: results` + verdict Gate PASS/FAIL.
- Phase C: stage duration, nhãn cost cap/budget, bỏ hardcode `/7 win`.
- Fix bug tự phát hiện: `AgentConsoleView is not defined` → console.jsx export `window.AgentConsoleView`, adapter đọc global + fallback.
- **Phase B: chọn đọc `context_summary` được cho là có sẵn trong event `prompt_built`, KHÔNG đổi pipeline/golden.** (← xem Review: giả định này sai.)
- `python -m pytest pipeline app/backend/tests -q` → 459 passed. Frozen DB hash giữ nguyên 64D989…C715.

## 6. Review *(Claude — verify độc lập)*

- **Verdict: Phase A + C = PASS. Phase B = REWORK (không đạt).**
- **Phase A PASS:** endpoint live trả TC=1.0 / TA=0.684211 / TA_REGISTRY=0.825843 / verdict pass / compare absent (đơn config S1). **Tự recompute cả 3 từ `score_run_final.json` đã commit → khớp từng số** (ref verify-on-committed-artifacts-not-reports). Read-only, 0-API, path-guard, thiếu report → present:false (không 500). Panel dùng đúng class có sẵn, render live (`term consistency 1.000 / gold adherence 0.684 / registry adherence 0.826`, banner Gate PASS). 37 route test pass. **Frozen hash 64D989…C715 nguyên**.
- **Phase C PASS:** duration hiện (11s…3m42), cost relabel, progress "?" khi chưa biết total. Fix global lành, console không kẹt loading, 0 lỗi.
- **Phase B REWORK:** claim "prompt_built đã có context_summary" **SAI**. Quét mọi event của run_214651ffe3d5: tất cả event translator payload chỉ có `{src_event_id, src_seq, src_stage_event_log_path}` — KHÔNG có pack/context field. `consolePackMessage` luôn null → fallback "prompt dựng xong"; UI xác nhận không dòng pack. CodeX nhầm `context_summary` của sidecar translate-run event (Cockpit) với merged one-button log (Console đọc). Bẫy green-tests-can-hide-dead-integration. Renderer forward-compatible (vô hại, giữ lại), nhưng **giá trị chưa giao**.
- **Follow-up: TASK_APP_E04** — translator emit `pack_summary` vào merged one-button event (từ context_audit) + cập nhật golden fixture + smoke; frontend đã sẵn sàng, chỉ cần dữ liệu.
