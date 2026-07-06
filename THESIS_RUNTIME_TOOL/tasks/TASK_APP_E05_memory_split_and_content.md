# TASK_APP_E05_memory_split_and_content — Hiện injection_action split + nội dung memory (không chỉ size)

- **Status:** DONE · PASS (verify Claude 2026-07-07)
- **Refs:** TASK_APP_E04 (pack_summary vào merged event — nền của task này) | pack-exclusion-injection-action-gate (3 mode hard/soft/excluded) | translator-pack-one-form-anchor (pack = 1 form/term) | prompt-memory-design-is-first-class (bounded, anchored, không dump full-registry) | two-event-streams-onebutton-vs-cockpit (Console đọc merged log)
- **Branch/Commit:** (điền khi imple xong)
- **Người làm:** CodeX implement · Claude review độc lập

## 1. Bối cảnh & mục tiêu *(Claude viết)*

E04 đã đưa `pack_summary` vào merged event → Console hiện dòng `window N · pack {inj} inj (0 excl) · {drop} drop · {tok}tok`. Nhưng dòng này **rút gọn lõi luận văn thành 1 con số** và có **1 số GIẢ**:

- `inj` = tổng term bơm, **giấu cấu trúc** injection_action (mandatory vs soft vs preserve). Đây đúng là "prompt = memory design" — hiện đang vô hình.
- `(0 excl)` là **hardcode**: `context_builder.py::_context_pack_summary` trả `"excluded_count": 0` cứng (dòng ~899). Đây là **số giả hiển thị như dữ liệu thật** — phải sửa hoặc bỏ (validity).

**Đã verify trên run thật `run_214651ffe3d5`** (query `workdb.sqlite3::memory_packs.payload_json → context_pack`): split **không tầm thường**, mỗi window đều có cấu trúc thật:

| window | mandatory (glossary) | soft (context_sensitive) | preserve | = injected |
|---|---|---|---|---|
| 1 | 16 | 2 | 2 | 20 |
| 2 | 11 | 2 | 12 | 25 |
| 3 | 14 | 3 | 1 | 18 |
| 4 | 7 | 2 | 5 | 14 |
| 5 | 11 | 1 | 3 | 15 |
| 6 | 8 | 0 | 2 | 10 |
| 7 | 1 | 0 | 3 | 4 |

Term thật (win1): mandatory `deep learning -> học sâu`, `model -> mô hình`; soft `tools -> công cụ (context-sensitive; do not force)`; preserve (do-not-translate). → **Story hiện ra**: mandatory ép cứng (nhất quán), soft cho linh hoạt cục bộ, preserve giữ nguyên. Đó là thiết kế memory làm nên luận văn.

**Mục tiêu:** (B1) làm dòng MEMORY hiện **split thật** thay cho `inj (0 excl)`; (B2) thêm panel **nội dung memory** liệt kê anchor line thật per bucket, **có bound**.

## 2. Nguồn sự thật — MAP, KHÔNG đếm lại bằng tay

`ContextPack` (`pipeline/retrieval/context_builder.py`, dataclass ~dòng 54) đã có sẵn field:
- **mandatory** ← `len(glossary_lines)` (+ `entity_lines` nếu có; run này entity=0)
- **soft** ← `len(context_sensitive_lines)`  (bucket "do not force" — chính là dòng 85 in prompt)
- **preserve** ← `len(preserve_lines)`
- **quarantine** ← `len(repair_queue)` (đã nằm trong ContextPack, review_only)
- **address** ← `len(address_lines)`

**Bất biến bắt buộc (acceptance sẽ assert):** `injected (== included_count hiện tại) == mandatory + soft + preserve + entity + address`. Đã đúng cả 7/7 window ở bảng trên.

**Excluded (report_only / deprioritize)** KHÔNG được giữ trong ContextPack (bị lọc bỏ ở `_glossary_items` dòng ~392). Đã có **hàm thuần `pack_policy_counts(term_rows)`** (context_builder ~dòng 180) tính đủ partition: `hard_translate / preserve / context_sensitive / report_only / repair_queue / pack_total / notebook_total`. → Để "excl" thật: cho `build_context_pack` surface `pack_policy_counts` (nó có `term_rows` nội bộ) rồi gắn vào ContextPack. **KHÔNG viết logic đếm mới.**

## 3. Spec

### B1 — Split thật trong dòng MEMORY *(core, must-have)*
1. `_context_pack_summary` / `_pack_summary_for_event` (`pipeline/translate/runner.py`) thêm khóa từ ContextPack: `mandatory, soft, preserve, quarantine` (và `address` nếu >0). **Thay** `excluded_count: 0` hardcode bằng: (a) số report_only thật từ `pack_policy_counts` nếu surface được, HOẶC (b) **bỏ hẳn `excluded`** khỏi payload — KHÔNG để 0 giả.
2. Field chảy qua merger E04 (`_stage_event_payload` đã copy `pack_summary` nguyên khối — kiểm nó không strip khóa mới).
3. `console.jsx::consolePackMessage` (đã hỗ trợ `mandatory`/`soft` dòng 75-76) — thêm `preserve`, `quarantine`. Dòng thành:
   `window 1 · pack 20 inj (16 mand / 2 soft / 2 preserve) · 0 drop · 176tok`
   (bucket = 0 thì bỏ khỏi ngoặc; đã là pattern sẵn.)

### B2 — Panel "nội dung memory" *(bounded)*
Console đọc merged log, nhưng anchor line THẬT nằm ở `memory_packs.payload_json` (không có trong merged log). Chọn **cách A (mặc định, đơn nguồn):**
- `pack_summary` mang thêm **mẫu có bound** các anchor line per bucket: `sample: { mandatory: [..≤6], soft: [..≤6], preserve: [..≤6] }` + `more: {mandatory: K, ...}` cho phần dư. Line đã là **1 form/term** (đúng translator-pack-one-form-anchor). Bound cứng ≤6/bucket (prompt-memory-design: neo + có trần, không dump full-registry).
- Console: dưới mỗi window (hoặc window đang chọn / latest) 1 expander "memory content" liệt kê sample theo nhóm, tái dùng class `.kv-row/.watch-row` sẵn có — **KHÔNG vẽ skin mới**.

*(Cách B thay thế nếu CodeX thấy event phình: 1 endpoint read-only kiểu E1/E2/E3 `/thesis/runs/<id>/memory-pack?window=` đọc `memory_packs`, Console fetch khi expand. Ghi rõ nếu chọn cách này.)*

## 4. Scope

- **IN:** surface split + bounded content (read-only từ ContextPack/memory_packs đã dựng); sửa số excl giả; render Console; golden fixture + test cập nhật theo số THẬT.
- **OUT:**
  - KHÔNG đổi logic build pack / KHÔNG đổi nội dung prompt thật gửi LLM. Chỉ ĐỌC & surface.
  - KHÔNG dump full-registry; content phải bound ≤6/bucket.
  - KHÔNG bịa `excluded` nếu chưa surface được — bỏ khỏi payload, đừng để 0.
  - KHÔNG skin/CSS mới — tái dùng class hiện có.

## 5. Acceptance criteria *(lệnh chạy được — SOI DỮ LIỆU THẬT, không chỉ test xanh)*

```bash
# 1) Bất biến split == injected trên run thật (đọc memory_packs, không tin UI):
python - <<'PY'
import sqlite3, json
db="THESIS_RUNTIME_TOOL/data/jobs/one_button_preface/one_button/run_214651ffe3d5/workdb.sqlite3"
c=sqlite3.connect(db); c.row_factory=sqlite3.Row
exp=[(16,2,2,20),(11,2,12,25),(14,3,1,18),(7,2,5,14),(11,1,3,15),(8,0,2,10),(1,0,3,4)]
i=0
for r in c.execute("SELECT payload_json FROM memory_packs ORDER BY rowid"):
    cp=(json.loads(r["payload_json"] or "{}").get("context_pack") or {})
    if not cp: continue
    m=len(cp.get("glossary_lines") or []); s=len(cp.get("context_sensitive_lines") or []); p=len(cp.get("preserve_lines") or [])
    e=len(cp.get("entity_lines") or []); a=len(cp.get("address_lines") or [])
    assert (m,s,p,m+s+p+e+a)==exp[i], f"win{i+1} got {(m,s,p,m+s+p+e+a)} exp {exp[i]}"
    i+=1
print("OK split invariant 7/7:", exp)
PY

# 2) Merged event mang split thật + KHÔNG có excluded=0 giả:
python - <<'PY'
import json
p="THESIS_RUNTIME_TOOL/data/jobs/run_events/<NEW_OR_REMERGED_RUN>.jsonl"
for l in open(p,encoding="utf-8"):
    r=json.loads(l) if l.strip() else {}
    if r.get("event")=="prompt_built":
        ps=(r.get("payload") or {}).get("pack_summary") or {}
        assert "mandatory" in ps and "soft" in ps and "preserve" in ps, ps
        assert ps.get("excluded",None) != 0 or "excluded" not in ps, "excluded=0 gia van con"
        print("OK", ps.get("mandatory"),ps.get("soft"),ps.get("preserve"))
PY

# 3) Console: dòng hiện "20 inj (16 mand / 2 soft / 2 preserve)"; expander memory-content liệt kê anchor line thật
#    (deep learning -> học sâu ...). console_dev golden vẫn render.
python -m pytest THESIS_RUNTIME_TOOL -k "pack or context or event" -q     # PASS
python -m pytest THESIS_RUNTIME_TOOL/pipeline THESIS_RUNTIME_TOOL/app/backend/tests -q   # PASS

# 4) Frozen DB hash KHÔNG đổi; emit read-only 0-API; golden fixture khớp 1 run thật (không hardcode số đẹp).
```

## 6. Implementation notes *(CodeX điền)*

- Chọn cách A hay B cho content; số bucket surface được (mandatory/soft/preserve/quarantine/excluded?); có phải sửa `build_context_pack` để lấy report_only không; dán nguyên văn output 4 lệnh acceptance.

## 7. Review *(Claude điền)*

- **Verdict: PASS** (verify độc lập 2026-07-07, không tin report).
- **Check quyết định (đóng cả 2 bẫy):** dựng lại `ContextPack` từ `b6_real/workdb.sqlite3::memory_packs` (7 window) → feed vào **chính hàm production `_pack_summary_for_event`** → output KHỚP golden fixture 7/7 **mọi field** (injected/mandatory/soft/preserve/quarantine/address + `sample` + `more`). Chứng minh production-path sinh đúng số (không chết) VÀ fixture là số thật (không bịa).
- **Bất biến** `injected == mandatory+soft+preserve+address` đúng 7/7 trên cả `b6_real` (fixture) lẫn `run_214651ffe3d5`.
- **Số giả đã sạch:** `excluded_count: 0` hardcode xóa khỏi `_context_pack_summary` (2 nhánh) + `_pack_summary_from_context_summary` (merger); fixture không còn key `excluded` (assert trong test).
- **Test không rỗng:** `test_pack_summary_for_event_maps_context_pack_counts` assert đủ dict split+sample+more; golden test assert invariant + no-excluded + có sample mandatory. 28 focused test PASS.
- **An toàn:** frozen DB hash `64D98965...B555C715` KHÔNG đổi · 0-API · read-only (chỉ surface ContextPack đã dựng).
- **Console render thật (console_dev):** dòng `window 1 · pack 20 inj (16 mand/3 soft/1 preserve) · 0 drop · 176tok`; panel `:: memory content` liệt kê anchor thật (`deep learning -> học sâu`, `model -> mô hình (do not force)`, `HTML (keep unchanged)`), bound 6, dùng `.watch-row` (0 CSS mới).
- **Follow-up (không chặn):** `report_only/excluded` chưa surface (CodeX chủ động BỎ field thay vì để 0 giả — đúng spec). Nếu demo muốn số "loại khỏi pack", mở task nhỏ nối `pack_policy_counts(term_rows)` vào `build_context_pack` (đã là hàm thuần sẵn).
