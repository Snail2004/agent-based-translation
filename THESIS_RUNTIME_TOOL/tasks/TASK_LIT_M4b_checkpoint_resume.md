# TASK_LIT_M4b — Checkpoint/resume cấp chương [rev2 sau review CodeX: 9 findings, nhận 1-9]

## Quyết định thứ tự (user chốt)
Checkpoint TRƯỚC M4 full — run ch1-4 tự ghi checkpoint, scale ch5+ resume từ đó.

## Phát hiện quan trọng kèm theo (CodeX review, Claude verify trên code — CONFIRMED)
**M2 hiện có FUTURE-LEAK:** `_chapter_roster_from_m1(m1_report)` lấy ledger CUỐI của toàn M1 rồi đưa
cùng roster vào MỌI chương digest → digest ch1 thấy nhân vật chỉ xuất hiện ở ch4. Vi phạm as-of.
Checkpoint chính là cơ chế sửa: M2 chương N đọc M1-checkpoint AS-OF chương N. Task này do đó không
chỉ chống chạy-lại mà còn sửa validity của B3.
**Dead code:** `previous_summary` trong run_m2 được gán nhưng call luôn truyền `""` — hành vi thật
của B3-context là neighbor k=2 từ digest summaries. Task này MÔ TẢ ĐÚNG hành vi thật, KHÔNG rewire
previous_summary (đổi nội dung prompt = task riêng sau, có gate riêng).

## Thiết kế

### Vị trí & namespace (finding 1)
- `<out_dir>/checkpoints/m1/<chapter_id>.json` và `<out_dir>/checkpoints/m2/<chapter_id>.json`.

### Nội dung checkpoint chương N
- **m1**: entity_ledger as-of cuối N; chapter_summaries tích lũy ≤N (neutral_premise, nguồn neighbor
  M1); cast_seed_report N; validation_counts RIÊNG N; accounting RIÊNG N (calls/attempts/tokens/cost).
- **m2**: digest summary N + chapter_summaries digest tích lũy ≤N (nguồn neighbor M2, k=2 — hành vi
  thật); validation_counts + accounting riêng N; **`input_m1_checkpoint_hash`** = checkpoint_hash của
  m1/<N> — M1 đổi thì checkpoint M2 tự mất hiệu lực (finding 2).
- **Chuỗi tin cậy (finding 4):** mỗi checkpoint có `checkpoint_hash` (hash canonical toàn nội dung),
  `parent_checkpoint_hash` (chương liền trước cùng stage, null với chương đầu), `artifact_manifest` =
  list {path, sha256, size} của brief/lexicon/narrative (m1) hoặc digest (m2) chương đó.
  Checkpoint chỉ PUBLISH sau khi toàn bộ artifact chương đã validate sạch; đang viết dở nằm ở
  thư mục tạm theo chương rồi promote (os.replace) khi hoàn tất.

### Hash hợp lệ (finding 5)
- `source_hash`: block_ids + clean_text chương.
- `prompt_hash`: hash prompt SAU RENDER per-chapter (`load_system_prompt_for_chapter`, tức sau
  `.replace("bk_ch01", chapter_id)`) cho từng version marker của stage.
- `config_hash`: model, temperature, seed, reasoning_effort, verbosity, response_format,
  max_output_tokens, window target_tokens/max_blocks, prompt_token_cap, neighbor K, pack-policy
  version.
- `schema_version`: hằng số RIÊNG cho m1 và m2, bump tay khi validator đổi hành vi.

### Resume (finding 3)
- Flag `--resume`, mặc định TẮT. Giao diện CHỐT: **luôn truyền danh sách chương đầy đủ từ chương 1**
  (vd `--chapters wh_ch01..wh_ch34 --resume`); chương có checkpoint hợp lệ (hash + manifest sha256
  khớp + parent chain liền mạch) được SKIP, restore state, chạy tiếp từ chương đầu tiên thiếu/invalid.
- Invalid giữa chuỗi ⇒ mọi chương sau re-run (parent chain đứt là re-run, không skip lỗ chỗ).
- Nếu danh sách yêu cầu không bắt đầu bằng chương đầu document và chương đầu danh sách không có
  parent-chain checkpoint hợp lệ ⇒ từ chối chạy với thông báo rõ.

### Lock chống double-writer (finding 7)
- `<out_dir>/checkpoints/lock.json` chứa pid/host/start_time; process mới từ chối nếu owner còn sống
  (kiểm pid tồn tại cùng host); owner chết ⇒ takeover + ghi log. Xoá lock khi kết thúc sạch.

### Accounting resume (finding 9)
- Report resume tách 3 phần: `restored_total` (từ checkpoint), `this_attempt` (lần chạy này),
  `combined_total`. Mỗi chương ghi `resumed_from_checkpoint` | `ran`.

### Không đổi
- `--resume` tắt = hành vi hiện tại + thêm ghi checkpoint. KHÔNG sửa prompt. KHÔNG rewire
  previous_summary. run_m3 (B4) không checkpoint — 0-API idempotent recompute; NHƯNG run_m3 sau task
  này phải nhận as-of inputs từ checkpoint m1/m2 khi có (tối thiểu: giữ nguyên hành vi hiện tại,
  as-of cho m3 sẽ chốt ở task M4-full).

## Acceptance (0-API — Claude verify lại độc lập)
A. Unit tests: mỗi loại hash lệch ⇒ invalid; prose-ngoài-blockquote đổi ⇒ VẪN hợp lệ; render-replace
   đổi chapter_id ⇒ prompt_hash khác nhau giữa các chương là ĐÚNG (hash theo chương); longest-valid-
   prefix + parent-chain đứt ⇒ re-run từ đó; tmp-file bỏ dở không được nhận; manifest sha256 lệch ⇒
   invalid; lock: owner sống ⇒ từ chối, owner chết ⇒ takeover.
B. Crash-sim dry-run: (i) chạy liền ch1+ch2 vs (ii) chạy ch1 → kill → resume. So sánh CANONICAL
   semantic state (json sort_keys, UTF-8, NFC, LOẠI accounting/timestamp/path — finding 8):
   entity_ledger, chapter_summaries, và CHUỖI REGISTRY_CONTEXT_PACK render cho window đầu ch2 —
   phải bằng nhau.
C. Replay rẻ: chạy ch1+ch2 checkpoint bật, dùng lại cache m4a ⇒ ~$0; checkpoint m1/wh_ch02 phải khớp
   entity_ledger của m1_report m4a đã gate (so canonical, bỏ accounting).
D. **As-of test cho M2 (finding 2):** dry-run/fixture 2 chương trong đó ledger cuối có entity chỉ
   thuộc ch2 ⇒ roster đưa vào digest ch1 KHÔNG chứa entity đó (đọc từ checkpoint m1/ch1); digest ch2
   thì có. Đây là acceptance mới, bắt buộc.
E. Tests literary + full pipeline pass; frozen DB hash nguyên; design doc không diff.

## Sau khi PASS
M4 full: run_m1 ch1-4 (--resume, tái dùng checkpoint/cache m4a) → run_m2 ch1-4 (as-of roster từ
checkpoint) → run_m3 → gate B4 (dup Heathcliff, Hareton inscription→person, king_lear allusion,
narration frame ch4, daughter-in-law phase) → checkpoint đã sẵn cho scale ch5+.

---
## GATE VERDICT (Claude verify độc lập, 2026-07-10): **PASS CÓ ĐIỀU KIỆN — 1 fix bắt buộc trước commit**

Đã tự verify: 351/351 tests pass (tự chạy); design doc không diff; scope đúng 4 file + 5 test mới;
frozen DB hash tự tính MATCH; crash-resume test so canonical ledger/summaries/pack trên đường
production thật; as-of test assert ở mức PROMPT RENDER thật (ent_bob không xuất hiện trong digest
prompt ch1) — đúng acceptance B và D, không phải test stub.

### FIX BẮT BUỘC: đóng cửa sau future-leak ở nhánh single-chapter
`_m1_checkpoint_chain_for_m2` có escape hatch `len(selected) == 1` → return [] khi thiếu checkpoint
→ run_m2 rơi về `fallback_roster = _chapter_roster_from_m1(m1_report)` = LEDGER CUỐI TOÀN M1.
Kịch bản leak thật: m1_dir legacy nhiều chương (vd m4a ch1+2, chưa có checkpoint) + gọi run_m2 cho
RIÊNG ch1 ⇒ digest ch1 thấy entity ch2 (Zillah, Mrs. Heathcliff) — future-leak quay lại đúng bug
finding 2 định giết. FIX: fallback chỉ hợp lệ khi m1_report["chapters_selected"] == đúng danh sách
1 chương đang digest (ledger cuối == as-of khi và chỉ khi M1 chỉ chạy chương đó); mọi trường hợp
khác RAISE như nhánh nhiều chương. Kèm 1 test: m1_report 2 chương + run_m2 1 chương không checkpoint
⇒ ValueError (không silent fallback).

### Đã đồng ý các deferral trung thực của CodeX
- Acceptance C (replay thật ch1-2 trên cache m4a) chưa chạy — ĐÚNG quy trình (cache miss có thể gọi
  API, cần gate explicit). Chạy ở bước kế: replay M1 qua cache để SINH checkpoint cho artifact m4a,
  điều kiện tiên quyết của M2 as-of. ~$0 nếu cache hit; trần chấp nhận ~$0.10 nếu miss.
- --confirm-usd estimate chưa trừ prefix skip: bảo thủ, không sai dữ liệu — cải thiện trước scale 34.
- .tmp/.testtmp là pytest artifact kẹt ACL — không commit.

### Sau fix + test mới pass
Commit batch M4b → chạy acceptance C → M4 full (ch1-4: m1 --resume → m2 as-of → m3) → gate B4.
