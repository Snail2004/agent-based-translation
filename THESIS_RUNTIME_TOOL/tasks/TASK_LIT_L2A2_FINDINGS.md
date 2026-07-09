# LITERARY L2A2 — Findings & locked decisions (Builder văn học, single-chapter)

Trạng thái: Builder trích xuất Story Bible chạy thật, gated PASS ở mức MỘT chương trên HAI tác phẩm
(Wuthering Heights ch1 + The Great Gatsby ch1). Đây là bản đóng gói tri thức của giai đoạn L2A2, làm
cơ sở cho chương method/ablation và để nối tiếp M4 (đa chương). Mọi số dưới đây đo trên artifact thật,
định hướng (1 chương), không có ý nghĩa thống kê hình thức.

## Kiến trúc Builder (4 bước + B0)
- B0 `literary_chapter_brief_v1`: đọc CẢ chương một lần → cast_on_stage (kèm surface_kind
  proper_name/descriptor), setting, scenes_party_size (co_present_count), neutral_premise. Framing toàn
  cảnh, KHÔNG kết luận quan hệ.
- B1 `literary_lexicon_v1`: glossary + character_mentions cục bộ theo window.
- B2 `literary_narrative_v1`: speaker_turns + relation_events cục bộ (bằng chứng, KHÔNG kết luận pha).
- B3 digest / B4 consolidation: gom chương/timeline (B4 = quyền cuối, danh tính+pha).
- Prompt nạp runtime từ blockquote trong `design/LITERARY_PROMPT_DESIGN.md` theo version-marker; chỉ
  blockquote tới model. Book-neutrality = luật + ví dụ đều phải trung tính (verify bằng loader thật +
  grep hai bộ token WH/Gatsby).

## Quyết định đã KHÓA (kèm bằng chứng)
| Quyết định | Giá trị | Bằng chứng đo được |
|---|---|---|
| Active window B1/B2 | target_tokens=500, max_blocks=8 | A/B 500 vs 1500: 1500 tiết kiệm ~16% chi phí nhưng recall mentions 67% / turns 50% / events <45% < sàn 95%; ~83% mất là window-size (lost-in-the-middle), ~17% do retry. |
| Temperature (extraction) | 1.0 | A/B 3+3 run WH ch1: temp0.2 ổn định membership hơn (membership Jaccard ~0.505→~0.602 (events = trục nhiễu, tùy chuẩn hóa evidence_quote)) + retry −75% nhưng recall turns 78.9% / events 53.8% < 95% + fail critical case (dog b018, sir b027). Temp = núm precision/recall↔nhất quán; recall thắng. Variance của 1.0 GẮN với recall (khám phá = tìm được evidence). |
| reasoning_effort | none | reasoning>none ăn max_output_tokens + trên gpt-5.4-mini ÉP temp về 1.0 (chỉ none mới cho chỉnh temp); không phải đòn tăng chính xác. |
| Xử lý lỗi validator | sửa-field-giữ-item + đếm; retry EDIT-based (không regenerate) | retry regenerate từng nuốt evidence thật (attempt-1 11 event → final 8). Pronoun→drop+count; named+ids→normalize; attribution_method enum→normalize field, giữ turn (FIX 4 cứu 4 turn + 3 event). |
| Seed cold-start | seed ledger từ B0 cast surface_kind==proper_name, provenance chapter_brief_cast, surface_evidence_block=null | đóng lỗ narrator "I"→Nick (Gatsby); code chỉ lọc cơ học theo cờ B0, KHÔNG hardcode tên. |

## Nguyên tắc xuyên suốt (bài học phương pháp)
1. **Recall là ưu tiên bất khả xâm phạm của Builder.** Mọi đòn bẩy chi phí/ổn định chỉ nhận khi giữ ≥95%
   recall của baseline. Sàn này chặn window 1500 lẫn temp 0.2.
2. **Validator không được thấy recall** — window/temp "pass validation" mà vẫn mất evidence; phải đo
   membership (Jaccard/set-diff theo block+surface), không tin count hay pass/fail.
3. **Sửa field, giữ item — không drop-whole-item, không regenerate.** Một ô schema sai (pronoun,
   named+ids, attribution enum) → chuẩn hoá đúng ô + đếm; regenerate là kẻ giết recall thầm lặng.
4. **Code không làm việc ngôn ngữ.** Phán tên-riêng/descriptor, coreference, pha → LLM; code chỉ lọc
   cơ học theo cờ + wordlist phổ quát (honorific), không hardcode tên nhân vật.
5. **Verify độc lập trên artifact thật, diff prompt trước mỗi gate.** Contamination Gatsby v2 (ví dụ
   prompt lấy từ chính chương test) chỉ lộ khi diff design doc với bản verify lần trước.

## Bề mặt CHƯA kiểm chứng (quan trọng nhất cho M4)
- **Bộ nhớ xuyên chương chưa từng chạy thật.** Cả hai run ch1 đều cold-start
  (REGISTRY_SO_FAR=none, NEIGHBOR_SUMMARIES=none). Neighbor-summary K=2 + registry-pack xuyên chương —
  lõi luận văn về memory — mới chỉ có code, chương 2 mới là lần đầu hoạt động.
- B4 watch-item: hợp nhất Miss Baker=Jordan Baker; dọn id sở hữu cách (ent_*_s / ent_*_and_*); rule
  narrator-được-đặt-tên mới fire nửa vời.

## Hướng chưa làm (tuỳ chọn, không chặn)
- json_schema Structured Outputs: ép field bắt buộc/enum lúc sinh → xoá lớp lỗi "quên utterance_gist".
- B1 named+ids normalize (lớp retry còn sót).
- Ablation window 4 điểm (500/750/1000/1500) — để vẽ đường recall-vs-window cho luận văn (không phải để
  đổi default; default 500 đã chốt).
- Prompt caching: đo headroom thật (đã cache một phần); đòn giảm chi phí an-toàn-recall duy nhất còn lại.

## Con số vận hành (WH ch1, tham chiếu)
Builder M1 1 chương ≈ 15–20 call, ~$0.03/chương ở 500/8/temp1.0. Amplification ~34x (source ~8k token →
API ~280k) vì 82% mỗi prompt là overhead cố định (system prompt 60%), chỉ 18% là source window.
