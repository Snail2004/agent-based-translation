# TASK OPT — Cost-optimization audit trên PROMPT RENDER THẬT (Builder M1+M2)

> **Trạng thái:** DRAFT (Claude, 2026-07-11). Ưu tiên SAU B4 (M4d). Đây là việc "thiết kế
> trước cho scale trả phí" — trên quota free hiện tại token cache VẪN đếm full nên chưa
> tiết kiệm quota/tiền, chỉ nhanh hơn; giá trị hiện hình khi Thầy/production chạy bằng tiền thật.
> **Model routing:** đo + phân tích = **Luna high** (cơ học, chạy/đếm). PROPOSE reorder được;
> nhưng VIẾT prompt mới là việc CLAUDE (prompt = lõi thesis, division-of-labor đã khoá).

## 0. LUẬT PHƯƠNG PHÁP (bất di, user chốt 2026-07-11)
Chỉ làm việc trên **prompt render THẬT** từ run đã commit (`literary_m4_full`) + `cached_tokens`
THẬT trong usage. **CẤM** ước lượng lý thuyết. Mọi finding phải trích **artifact call thật +
con số token thật**. Lý do: lý thuyết ≠ thực tế, dự đoán ≠ thực nghiệm. Ta ĐÃ có run thật →
đã có bản chất thật của hệ thống → soi khiếm khuyết THẬT, không tưởng tượng từ thiết kế.
Nguyên tắc "khai báo prompt kèm run thật để soi" áp cho MỌI task từ nay.

## 1. Baseline đã đo (Claude, verify lại + mở rộng)
Trên `literary_m4_full`, call API thật (bỏ replay local), server prefix-cache:
- **B0 brief: 0/2 cache** (0%). **B1 lexicon: 8/29** (~59% prefix khi trúng).
- **B2 narrative: 27/29** (~52% prefix — khung window đã bám cache TỐT).
- **M2 digest: 0/4** (gpt-5.4). Prefix cố định đo được: ~1.320 tok giống-hệt qua cả 49 window
  (vượt ngưỡng OpenAI 1.024). CodeX tái tạo bảng này per-stage per-call để chốt baseline.

## 2. Giải phẫu prompt trên bản RENDER THẬT (không đoán cấu trúc)
Mỗi stage, cắt prompt render thật thành segment (system preamble / schema / instruction /
example / roster / window-context-pack / block-text). Đo **token từng segment** + gắn nhãn
**STABLE** (giống hệt mọi call) hay **VARYING**. Từ đó chỉ ra:
- (a) **cache-breaker**: segment VARYING nằm TRƯỚC segment STABLE trong prefix → cắt cache sớm.
- (b) **thừa/bloat**: segment lặp, hoặc to hơn cần, cắt được mà KHÔNG giảm chất lượng.
- (c) **thiếu/gap**: nội dung đáng có mà chưa có.

## 3. Ba câu hỏi phải TRẢ LỜI bằng thực nghiệm (không suy đoán)
- **Q1. Endpoint gpt-5.4 có hỗ trợ server prompt-cache như mini không?** Test: 2 call
  cùng-prefix kiểu-M2 chạy liên tiếp, TẮT cache local, đọc `cached_tokens` call thứ 2.
  (M2=5.4=0 cache vs B1/B2=mini=cache — do endpoint hay do cấu trúc prompt?)
- **Q2. Vì sao B1 (8/29) cache tệ hơn hẳn B2 (27/29)** dù cùng windowed? Diff prefix render
  thật của một call B1 và một call B2, tìm chỗ B1 phân kỳ sớm hơn.
- **Q3. B0 & M2 = 0 cache** là do sửa được bằng reorder (đưa ổn-định-trong-chương lên trước
  đặc-thù-chương/window) hay do cấu trúc (quá ít call / input trọn-chương lấn prefix)?

## 4. ĐỀ XUẤT reorder — nhưng GATE bằng tương-đương-chất-lượng (điều kiện cứng)
Mọi reorder để tăng cache **PHẢI A/B chất lượng**: cùng input, prompt đảo thứ tự, verify output
**tương đương** (recall/validity/nội-dung không giảm). Lý do: đảo vị trí segment có thể đổi hành
vi model (prefix-cache cần prefix byte-identical, nhưng dời segment làm đổi attention). **CẤM**
ship reorder tăng cache mà giảm extraction. Cán cân: lợi cache KHÔNG được đánh đổi chất lượng
(hoặc chỉ giảm không đáng kể — user chốt "cán cân hợp lý"). CodeX chỉ ĐO cache-gain + chạy A/B;
Claude VIẾT prompt reorder thật.

## 5. Bloat trim
Segment nào bị chỉ là thừa (§2b) → đề xuất cắt + A/B recall-floor không đổi. Nhớ bài học cũ:
window 500/8 đã khoá vì 1500/24 phá recall floor — cắt token KHÔNG được chạm floor.

## Deliverable
Report trích **artifact call thật**: bảng anatomy per-stage, trả lời Q1-Q3 kèm bằng chứng,
mỗi đề xuất kèm CẶP SỐ (cache-gain, quality-delta). Claude gate độc lập trước khi wire; KHÔNG
wire gì khi chưa có Claude verify + user sign-off (prompt = Claude-owned). D2L-intact bắt buộc.
