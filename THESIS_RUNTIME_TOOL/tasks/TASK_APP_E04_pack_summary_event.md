# TASK_APP_E04_pack_summary_event — Emit pack_summary vào merged one-button event để Console hiện dòng MEMORY

- **Status:** READY
- **Refs:** TASK_APP_E03 (Phase B trơ — renderer đã merge) | TASK_RUN_EVENT_01 (sidecar event schema) | TASK_APP_B01 (Cockpit MemoryPackInspector — nguồn field context_audit) | pack-exclusion-injection-action-gate | prompt-memory-design-is-first-class
- **Branch/Commit:** (điền khi imple xong)
- **Người làm:** CodeX implement · Claude review độc lập

## 1. Bối cảnh & mục tiêu *(Claude viết)*

TASK_APP_E03 Phase B đã thêm renderer `consolePackMessage` (console.jsx) để hiện 1 dòng/window cho biết pack đã bơm gì. **Nhưng nó đang TRƠ**: Claude verify (quét mọi event của `run_214651ffe3d5`) thấy **merged one-button event log KHÔNG mang pack/context field** — mọi event vòng đời translator (`prompt_built`, `window_preview_available`, `request_sent`…) payload chỉ có `{src_event_id, src_seq, src_stage_event_log_path}`. Nên `consolePackMessage(p.pack_summary || p.context_summary)` luôn null → fallback "prompt dựng xong".

Dữ liệu pack có tồn tại nhưng ở **nguồn khác**: bảng `memory_packs.context_audit` (Cockpit đọc: `included_count / excluded_count / dropped_by_budget_count / anchors_count / estimated_tokens`) và sidecar translate-run event của luồng Cockpit — KHÔNG phải merged one-button log mà Console tiêu thụ.

Mục tiêu: cho translator (trong luồng one-button) **emit `pack_summary`** vào merged event stream cho mỗi window, để renderer sẵn có tự sáng lên. Đây là lõi luận văn (prompt = memory design) hiện đang vô hình ở màn demo.

## 2. Scope

- **IN:** emit `pack_summary` per-window vào merged one-button event; cập nhật golden fixture + test; smoke khẳng định field xuất hiện & Console render.
- **OUT:**
  - KHÔNG sửa renderer frontend (đã xong ở E03; `consolePackMessage` đọc `pack_summary` sẵn). Chỉ VERIFY nó render.
  - KHÔNG đổi logic build pack / không đổi nội dung prompt. Chỉ ĐỌC `context_audit` đã có và đính vào event.
  - KHÔNG bịa mandatory/soft nếu chưa tách được theo injection_action — để null (renderer đã handle null: chỉ hiện injected/excluded/dropped/tokens).
  - KHÔNG đổi skin/CSS.

## 3. Spec *(Claude viết)*

### Nguồn field (map 1-1, không tính toán lại)
Từ `memory_packs.context_audit` (hoặc struct pack tương đương mà translator đã dựng cho window):
- `injected`  ← `included_count`
- `excluded`  ← `excluded_count`
- `dropped_by_budget` ← `dropped_by_budget_count`
- `est_tokens` ← `estimated_tokens`
- `mandatory` / `soft`: chỉ set nếu pack có split theo `injection_action` (ref pack-exclusion-injection-action-gate: hard MANDATORY vs soft own-section). Chưa có → để null (bỏ khỏi payload hoặc null).

### Emit ở đâu
1. CodeX phải **trace đường event**: stage translator ghi `run_dir/stage_events/translate.a1.jsonl` → orchestrator merge sang `run_dir/../run_events/<run_id>.jsonl` (merged log Console đọc). Xác định event per-window nào phù hợp nhất để đính pack (`prompt_built` là hợp lý nhất — pack dựng xong ngay trước khi build prompt) **hoặc** emit event mới `pack_built` ngay sau khi dựng pack.
2. Đính `pack_summary` vào `payload` của event đó. Bảo đảm field **sống sót qua bước merge** (nhiều event hiện chỉ còn `src_*` sau merge — kiểm tra merge không strip payload; nếu merge lược payload thì phải cho pack_summary vào allowlist giữ lại).
3. Pack dựng **trước khi gọi LLM** → emit này **0-API**, test được không cần API thật.

### Frontend (chỉ verify, không sửa)
`console.jsx` `consoleMessageFor` case `prompt_built`/`pack_built` đã gọi `consolePackMessage(p.pack_summary || p.context_summary || p, ctx)`. Khi event có `pack_summary`, feed hiện:
```
window {n} · pack {injected} inj ({mandatory} mand/{soft} soft/{excluded} excl) · {dropped} drop · {est}tok
```
(mandatory/soft bị bỏ khỏi ngoặc nếu null — đã code sẵn.)

### Fixture & test (bắt buộc — nếu không thì Console_dev & test lệch)
- Cập nhật golden `fixtures/one_button_preface_golden/events.jsonl`: thêm `pack_summary` vào các event window tương ứng (đúng như output run thật).
- Cập nhật/thêm test event-schema khẳng định `pack_summary` có mặt + đúng key.
- Nếu normalizer trong `console_dev.html` cần biết field mới thì cập nhật; nếu không, để nguyên (renderer generic).

## 4. Acceptance criteria *(lệnh chạy được)*

```bash
# 1) Chạy/replay 1 window translator (0-API nếu có cache) → merged event có pack_summary:
python - <<'PY'
import json,glob
paths=sorted(glob.glob("THESIS_RUNTIME_TOOL/data/jobs/**/*run_events*/*.jsonl",recursive=True))
hit=False
for p in paths:
    for l in open(p,encoding="utf-8"):
        r=json.loads(l) if l.strip() else {}
        if (r.get("payload") or {}).get("pack_summary"):
            print("OK pack_summary:", r["event"], r["payload"]["pack_summary"]); hit=True; break
    if hit: break
assert hit, "pack_summary KHÔNG xuất hiện trong merged event — vẫn trơ"
PY

# 2) test event-schema + golden xanh:
python -m pytest THESIS_RUNTIME_TOOL -k "event and pack" -v      # → PASS
python -m pytest THESIS_RUNTIME_TOOL/pipeline THESIS_RUNTIME_TOOL/app/backend/tests -q   # → toàn bộ PASS

# 3) Frontend: Console trên run mới hiện dòng "window N · pack ... inj ... drop ... tok"
#    (không còn chỉ "prompt dựng xong"); console_dev.html vẫn render.

# 4) Frozen DB hash KHÔNG đổi; emit là 0-API.
```

## 5. Implementation notes *(CodeX điền)*

- Đường event đã trace (file nào → merge ở đâu), event chọn để đính, có phải thêm allowlist giữ payload khi merge không. Output các lệnh acceptance (dán nguyên văn).

## 6. Review *(Claude điền)*

- **Verdict:** PASS / REWORK.
- Findings: verify độc lập bằng cách **quét merged event thật** (không chỉ tin test) khẳng định `pack_summary` có mặt & Console render; kiểm 0-API, frozen hash, golden khớp run thật (không hardcode số đẹp).
- Follow-up (nếu có): mở TASK mới.
