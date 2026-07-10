# TASK_LIT_M4a — Pilot liên chương đầu tiên: WH ch1+ch2 (M1 chain)

## Mục tiêu
Lần ĐẦU TIÊN kiểm chứng trí nhớ xuyên chương trên API thật: ledger + registry pack + neighbor
summary chảy từ ch1 sang ch2. Đây là bước đệm trước M4 full (ch1-4 + B3 + B4).

## Phạm vi — KHÔNG sửa code, KHÔNG sửa prompt
Cơ chế đã có sẵn trong run_m1 (ledger dùng chung, registry_context_from_ledger, neighbor k=2).
Đây là task CHẠY + BÁO CÁO thuần. Nếu có lỗi: báo về, không tự vá prompt/validator.

## Cách chạy
- run_m1 với chapters=["wh_ch01","wh_ch02"] trong MỘT invocation (bắt buộc — ledger chỉ carry
  trong cùng run), config llm_prepass.yaml (mini, temp 1.0, seed cũ), window 500/8 (mặc định).
- out_dir mới: data/reports/literary_m4a_wh_ch1_2/ ; cache file riêng cho run này.
- Ước lượng: ~30-34 call, ~$0.07 mini. Xác nhận estimate trước khi chạy như thường lệ.

## Báo cáo bắt buộc (Claude sẽ verify lại trên artifact)
1. Call/attempt/retry + token thật; counters validator (kỳ vọng retry ~2-5% sau L2A2g).
2. **Ledger continuity**: dump entity_ledger cuối run — các ent_* của ch1 còn nguyên khi vào ch2.
3. **Canary Hareton (điểm ăn tiền của pilot)**: ch1 có "Hareton Earnshaw" khắc trên cửa (ledger
   ent_hareton_earnshaw); ch2 Hareton BẰNG XƯƠNG THỊT tự xưng "My name is Hareton Earnshaw".
   Mention/turn ở ch2 phải NỐI vào ent_hareton_earnshaw CŨ (nhờ registry pack), không đẻ entity mới.
4. **Canary quan hệ ch2**: cảnh Lockwood đoán nhầm Mrs. Heathcliff là vợ Heathcliff và bị đính
   chính — relation_events phải bắt được evidence đính chính (đây là dữ liệu pha quan hệ thật).
5. **Không đẻ dup mới** cho Heathcliff/Lockwood (dup ent_heathcliff/ent_mr_heathcliff CŨ của ch1
   được phép tồn tại — nó là việc của B4, KHÔNG tự sửa ở M1).
6. Registry pack thật sự được bơm: trích 1-2 prompt B1 của ch2 cho thấy REGISTRY_CONTEXT_PACK
   có entity ch1 (không "(none yet)").

## Sau khi Claude gate PASS
Scale M4 full: run_m1 ch1-4 (một chain) → run_m2 (B3 digest chain) → run_m3 (B4 story bible, 0-API)
→ gate M4: merge dup Heathcliff + Hareton inscription→person + narration frame ch4 + desk-check
bản Dương Tường.

## Ghi chú wiring (đã đọc code, 2026-07-09)
Neighbor summary trong M1 = neutral_premise (B0) chương trước, KHÔNG phải chapter_rolling_summary
(B3) — B3 summary chỉ chảy trong chuỗi run_m2. Đúng thiết kế hiện tại; có nâng cấp hay không sẽ
quyết bằng bằng chứng M4, không đổi bây giờ.

---
## GATE VERDICT (Claude verify độc lập, 2026-07-09): **PASS**

Kiểm trên artifact thật (không nhận số báo cáo):
- Số khớp 100%: 42 call / 43 attempts / 1 retry = 2.4% (đúng dự đoán replay L2A2g trên DỮ LIỆU MỚI
  — lần validate đầu tiên của con số 2.4% ngoài tập replay), $0.0949, prompt/code sạch, config tạm
  lệch đúng 1 trường prompt_token_cap 6000→6500 (CHẤP NHẬN: pack + neighbor summary làm prompt ch2
  lớn hợp lệ; cap là guard, không phải prompt).
- **Liên chương HOẠT ĐỘNG — bằng chứng mạnh nhất KHÔNG phải Hareton mà là tái sử dụng id:** ch2
  narrative cite ent_mr_lockwood 41× và ent_joseph 2× — đều là id do CH1 tạo. REGISTRY_CONTEXT_PACK
  trong prompt thật của ch2 chứa 6 entity gồm các id ch1.
- **Canary daughter-in-law: ĐẸP.** b042 "Mrs. Heathcliff, your wife, I mean" (event 'corrects') +
  b046 "Mrs. Heathcliff is my daughter-in-law" (event 'states') — evidence pha quan hệ thật đầu tiên.
- **Hareton: caveat CodeX ĐÚNG, và mình đo được cụ thể hơn:** ch1 B1 CÓ bắt inscription (mention
  named b012, không id — đúng schema), nhưng ent_hareton_earnshaw sinh ra từ B0 seed CH2, nên ch2
  cite id này KHÔNG chứng minh nối ch1→ch2. Phép nối ch1-mention ↔ ch2-entity là việc của B4 (by
  surface) → chuyển canary Hareton thành acceptance của B4, giữ nguyên như đã ghi.

### Ghi chú cho M4 full (không chặn gate)
1. **Pack-rendering wart:** dòng pack in "seeded:chapter_brief_cast:no_surface_alias" vào cột alias
   (marker nội bộ leak thành text cho model đọc). Fix cơ học: alias rỗng thì bỏ cột, không in marker.
2. **Ledger fragments chờ B4** (đúng kế hoạch): ent_hareton/ent_earnshaw/ent_mr_earnshaw/
   ent_mr_and_mrs_heathcliff + dup Heathcliff. **ent_king_lear** = allusion văn học thành entity —
   thêm vào B4 acceptance: allusion không được thành nhân vật on-stage.
3. Counter mini 2 chương: named_ids_cleared=18, attribution_enum_normalized=14, nonperson=17 —
   scaffolding validator đang gánh đúng như đo ở ch1; không phải lỗi mới.

### Quyết định checkpoint/resume (đề xuất CodeX — ĐỒNG Ý, có điều chỉnh thứ tự)
- KHÔNG chặn M4 full: ch1-4 ≈ $0.15, chết giữa chừng chạy lại được, cache còn giảm nữa.
- BẮT BUỘC trước full-book 34 chương: checkpoint cấp chương atomic (entity_ledger + briefs +
  digest/rolling summary B3 + counters + source/prompt/config/model hash), resume skip chương done
  nếu hash khớp, hash lệch → re-run từ checkpoint hợp lệ gần nhất, mỗi mốc có report riêng để gate.
- Thứ tự: M4 full (ch1-4 + B3 + B4) TRƯỚC → checkpoint infra (TASK riêng, kèm tests + dry-run
  resume giả lập crash) → rồi mới scale 34 chương.
