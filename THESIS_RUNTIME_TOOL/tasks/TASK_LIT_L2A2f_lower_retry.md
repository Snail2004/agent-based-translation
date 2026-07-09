# TASK_LIT_L2A2f — Hạ retry rate trước M4  [rev2: FIX A + FIX C validator, json_schema thành tuỳ chọn]

Status: SPEC rev2. Gộp review CodeX + decomposition dữ liệu thật. Mục tiêu retry <10% mà KHÔNG mất
recall. Không đổi window/temp/nội-dung-prompt. Giữ FIX 1–4. Không commit.

## Dữ liệu thật (8 retry, 3 run temp=1.0, verify per-attempt)
Một retry chỉ biến mất nếu MỌI lỗi trong attempt đó được sửa. Phân loại theo attempt:
- 1/8: B1 named+candidate_ids.
- 2/8: thiếu field bắt buộc THUẦN (utterance_gist/block_id), không kèm lỗi khác.
- **5/8: có lỗi outside-window-block** → 2 fix gốc (B1-normalize + json_schema) KHÔNG chạm tới → sàn
  thực ~11%, KHÔNG đạt <10%.
Đã verify: **mọi block outside-window đều là block HÀNG XÓM** (active b015–b018 → trích nhầm b019/b020;
active b013 → b011/b012). Đây là leak từ tail READ-ONLY; mỗi block đó là ACTIVE của window kế bên nên
được trích ở window của chính nó → **drop-and-count entry đó là recall-safe toàn cục.**

## FIX A (validator, 0-API verify): B1 named+ids → normalize tại chỗ
Nhân bản logic named+ids của B2 sang validator B1: mention `status=named` + `candidate_entity_ids` không
rỗng → clear ids=[], giữ mention, đếm `mention_named_ids_cleared`, KHÔNG retry. Guard hẹp. → −1 retry.

## FIX C (validator, 0-API verify) — ĐÒN CHÍNH: outside-window-block entry → drop-and-count
Entry (mention/turn/event) có block_id KHÔNG thuộc active window → **drop RIÊNG entry đó + đếm**, KHÔNG
retry cả window. Đây đúng triết lý drop-and-count §1 (leak block = lỗi cấp-entry). Recall-safe vì block
hàng-xóm được xử ở window của nó. Đếm tách 2 loại:
- `outside_window_neighbor_dropped`: block tồn tại nhưng là tail/hàng-xóm (recall-safe, phủ ở window kia).
- `outside_window_nonexistent_dropped`: block không tồn tại trong chương (hallucinate — cũng drop, rác).

**AMENDMENT rev3 (Claude verify 2026-07-09, sau khi đọc bản implement chưa commit):** với item có
block_ids dạng DANH SÁCH (glossary_candidates, character_mentions), nếu danh sách HỖN HỢP (có block
in-window LẪN block neighbor) thì KHÔNG được drop nguyên item — phải **lọc bỏ block xấu, GIỮ item với
các block in-window còn lại** (+ đếm số block bị lọc). Drop-nguyên-item chỉ khi TOÀN BỘ block_ids đều
ngoài window (hoặc danh sách rỗng sau lọc). Lý do: mention "Joseph" [b013(in), b019(neighbor)] mà drop
cả item thì mất Joseph@b013 — window kế chỉ bù được Joseph@b019, KHÔNG bù được b013 → recall hole, đúng
vết fix-the-field-keep-item. Với speaker_turns/relation_events (block_id đơn) drop-nguyên-item là đúng
vì anchor duy nhất nằm ngoài. Bản implement hiện tại (`_outside_window_drop_kind` trong
builder_pilot.py) đang drop-nguyên-item cho cả case hỗn hợp → cần sửa trước khi chạy replay gate.
→ xoá 5/8 retry. Kết hợp FIX A: 6/8 → còn ~2 retry (thiếu-field thuần) = ~2/45 ≈ **4.4% < 10%**, KHÔNG
cần json_schema.

## FIX B (json_schema Structured Outputs) — HẠ ƯU TIÊN, TUỲ CHỌN
Sau A+C chỉ còn ~2 retry thiếu-field → payoff nhỏ. Chỉ làm nếu muốn "sạch cấu trúc" (ép field bắt
buộc/enum lúc sinh). NẾU làm, theo đúng thiết kế đo CodeX yêu cầu:
- A/B 3+3 interleaved, fresh cache riêng mỗi run (như temperature test), hai arm ĐÚNG:
  arm-json_object = FIX A+C + json_object; arm-json_schema = FIX A+C + json_schema. Chỉ đổi 1 biến.
- KHÔNG fallback giữa arm đo: probe TRƯỚC (1 call MỖI mode B0/B1/B2 — client đã dùng json_schema thành
  công ở localizer/reelection, rủi ro là schema từng-mode có hợp lệ không, B2 phức tạp nhất); nếu probe
  fail → dừng + báo, không chạy arm. Trong arm, nếu 1 call lỗi schema → abort arm, không trộn format.
- Strict OpenAI: mọi object `additionalProperties:false`; MỌI property trong `required` (tuỳ-chọn →
  nullable union `["string","null"]`); enum khai đủ; root object.
- json_schema CHỈ lo cấu trúc; validator ngữ nghĩa (block-in-window/FIX C, named⇒[]/FIX A, drop-count)
  VẪN giữ.
- "Field rác" định nghĩa CƠ HỌC (không chủ quan): `utterance_gist.strip() != ""`;
  `utterance_quote`/`evidence_quote` không rỗng VÀ (tốt hơn) normalized-containment trong text block
  nguồn (bắt cả bịa quote). Field rỗng required = lỗi, không nhận bừa.

## Verify & gate
- FIX A + FIX C: **replay raw attempt cũ 0-API** (dùng 3 run temp10 đã commit) → xác nhận đúng
  6/8 retry biến mất, counter tăng đúng lớp, và **không có entry recall-thật nào bị drop nhầm** (block
  drop phải là hàng-xóm/nonexistent, không phải active). Rồi 1 fresh real run WH ch1 xác nhận retry giảm.
- Gate (Claude, so consensus WH ch1 trước/sau trên toàn chương, KHÔNG chỉ đếm):
  retry <10%; recall consensus mentions/turns/events KHÔNG giảm (FIX C drop phải được window kế bù lại);
  critical sir×3 + dog b018 giữ; 0 placeholder/coined id; frozen hash 64D989…C715; tests xanh.
- Harness: `compare_windows.py` (hiện chỉ ở scratchpad) sẽ được đưa vào repo
  `pipeline/scripts/compare_literary_runs.py` để gate tái lập được (CodeX flag đúng: chưa có trong repo).

## Quyết định / thứ tự
1. FIX A + FIX C ngay (đều validator, 0-API verify, rẻ, recall-safe) → đạt ~4% retry. ĐỦ cho mục tiêu.
2. json_schema (FIX B): hoãn/tuỳ chọn — chỉ khi muốn hardening cấu trúc, và chỉ theo A/B 3+3 ở trên.
3. Nếu ưu tiên thời gian: FIX A + FIX C xong là vào M4 được; retry ~4% + edit-based repair là quá đủ.
Ghi chú trạng thái repo (sửa 2 claim): retryfix2 ĐÃ commit (c13743d); compare_windows.py CHƯA (sẽ đưa vào).


---
## GATE VERDICT (Claude verify độc lập, 2026-07-09): **PASS**
Recompute độc lập trên artifact thật (không nhận số báo cáo):
- Replay 0-API 3 run temp10: 42 window, 8 retry cũ → 5 tránh được → **còn 3 = 7.14%** (< 10%). Khớp CodeX từng số.
- 3 retry còn lại đều thiếu-field thuần (utterance_gist ×2, block_id required ×1) = đúng class FIX B tuỳ chọn.
- Mixed-block rev3 hoạt động đúng trên case thật (temp10_r2 wb_003: lọc b011/b012 neighbor, GIỮ item với b013).
- **Consensus recall (≥2/3 run) KHÔNG ĐỔI cả 3 trục: mentions 20=20, turns 19=19, events 13=13, lost=∅.**
  (3/5 window tránh-retry có attempt-1 ít item hơn attempt-2, nhưng toàn bộ phần hụt là item single-run
  ngoài consensus — retry ở temp=1.0 là variance hai chiều, không phải nguồn recall hệ thống.)
- Tests: 34 literary + 339 full pipeline pass (tự chạy). Design doc prompt không đổi. Frozen D2L DB
  SHA256 tự tính = 64D989…C715 MATCH.
→ FIX A+C đóng. M4 đủ điều kiện vào, chạy bằng gpt-5.4-mini.
