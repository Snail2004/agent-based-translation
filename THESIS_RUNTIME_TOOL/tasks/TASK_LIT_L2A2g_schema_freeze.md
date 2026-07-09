# TASK_LIT_L2A2g — Schema freeze trước scale + micro-change utterance_gist

## Bối cảnh
Review schema 3 bên (user + Claude + CodeX) trên dữ liệu thật (6 run WH ch1, Gatsby ch1, gpt-5.4).
Kết luận chung: B1/B2 KHÔNG thiếu trường sống còn; rủi ro lớn hơn là thêm trường vì tưởng tượng.

## QUYẾT ĐỊNH KHOÁ (2026-07-09)

1. **`utterance_gist`: required → optional, CHỈ ở validator.** PROMPT GIỮ NGUYÊN (không xoá field
   khỏi blockquote — prompt là baseline so sánh, mọi edit prompt bắt buộc re-gate; relax validator
   là thay đổi 0-API thuần). Model điền thì giữ, thiếu thì KHÔNG retry. Zero consumer đã verify
   bằng grep (chỉ xuất hiện ở example + required-list). Kỳ vọng retry replay: 3/42 → 1/42 ≈ 2.4%.
2. **`confidence`: giữ, DIAGNOSTIC-ONLY.** Đúng design doc dòng 47: "B4 gate theo METHOD, không theo
   confidence" — tín hiệu trust thật = attribution_method + roster-membership. Claude từng nói "B4
   dùng confidence làm trọng số được" → SAI so với chính design đã khoá; CodeX bắt đúng. Chỉ nâng
   cấp vai trò confidence sau khi có spot-check calibration thật.
3. **`termhood`: giữ nguyên, hiểu là review-note tự do.** Không viết logic trên nó. Đổi tên
   (term_note) hoãn — rename kéo migration/report không đáng trước scale.
4. **`scenes_party_size`/`co_present_count`/`register_cue`/`attribution_method`: giữ nguyên,**
   load-bearing (canary sir b005/b006 pass nhờ co_present==2 turn-alternation).
5. **`neutral_premise`: giữ + leak-guard vĩnh viễn** (precedent Gatsby contamination: field này dễ
   mớm đáp án nếu viết từ corpus đang test; gate step = diff design doc mỗi lần).
6. **KHÔNG thêm trước M4:** `speech_style` (per-character, tầng B3 — thêm sau desk-check Translator),
   plural addressee (watch-list, chờ failure thật), narrator-policy chi tiết (chờ ch4 chạy thật).
7. **`mention_function` (present|mentioned|inscription|historical_reference): WATCH-ITEM có trigger.**
   Bằng chứng thật đã có trong ledger gpt54_key2: `ent_hareton_earnshaw` (tên khắc cửa, KHÔNG on-stage,
   lọt vào ledger qua B1 named-promotion dù B0 đã loại đúng) + entity trùng `ent_heathcliff` vs
   `ent_mr_heathcliff`. TRIGGER thêm field: nếu M4 consolidation phải đoán nhiều ở các ca này.
   Trước mắt chúng là ACCEPTANCE CHECK của M4, không phải field mới của B1.

## Việc cần làm (CodeX)
- [ ] Validator: bỏ `utterance_gist` khỏi required-list của speaker_turns (giữ mọi thứ khác).
- [ ] Cập nhật test tương ứng (case thiếu gist → pass, không retry).
- [ ] Replay gate 0-API trên 3 run temp10 đã commit: báo lại old/avoided/remaining (kỳ vọng còn 1/42).
- [ ] KHÔNG sửa prompt, KHÔNG sửa field khác. Diff design doc phải rỗng.

## Sau đó
FREEZE B1/B2 schema → vào M4 (mini). M4 acceptance thêm 2 check từ ledger thật:
(a) merge đúng ent_heathcliff/ent_mr_heathcliff; (b) Hareton inscription-name không thành nhân vật
on-stage trước khi xuất hiện thật (ch2). Sau M4 + artifact B3/B4 thật: desk-check bản Dương Tường
(2-3 đoạn xưng hô/giọng khó) → gap có bằng chứng mới được thêm field.
