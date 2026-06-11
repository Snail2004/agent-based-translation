# TASK — S4: Căn nhãn entity/term theo vị trí trong preview (BONUS / experimental, chặng cuối)

**Loại:** frontend `AILAB_HANDOFF/app/prototype` (highlight trên màn hình S3). **Người làm:** CodeX. **Trạng thái:** chưa làm.

> **Khung an toàn:** vẫn là **viewer read-only**. S4 chỉ **tô sáng** nhãn entity/term trên văn
> bản đã render (S3); KHÔNG sửa dữ liệu, KHÔNG ghi `canonical/`/gold, KHÔNG backend mới.

## 0. Bối cảnh & vị trí roadmap

S1 (skill) → S2 (lưu run) → S3 (song song EN|VI mức block) → **S4 (task này, cuối cùng):**
tô sáng entity/term **đúng vị trí** trong khung VI (và EN), dùng `mentions` của preview run.
Sau S4, tính năng bonus S1–S4 hoàn tất.

## 1. Phạm vi

- **Thuần frontend** (`app/prototype`, chủ yếu `TranslationPreviewView` trong `parts_center.jsx` + `styles.css`).
- **Không backend, không API mới, không data mới** — chỉ dùng `preview.blocks[].mentions[]` đã có:
  `{entity_id|term_id, source_surface, target_surface}`.
- Không đổi schema/workflow.

## 2. Hành vi tô sáng (BEST-EFFORT, không phải span hoàn hảo)

- **Khung VI:** với mỗi mention, tìm `target_surface` (chuỗi) **trong `target_text`** → tô sáng đoạn khớp.
- **Khung EN:** với mỗi mention, tìm `source_surface` trong `clean_text` của block → tô sáng.
- **Không tìm thấy** (lược chủ ngữ / diễn đạt khác / surface rỗng) → **KHÔNG tô**, giữ nguyên chip
  `used_context` mức block như S3. **Không báo lỗi.**
- **Trùng/lồng nhau:** nếu nhiều `target_surface` chồng vị trí, ưu tiên **match dài nhất / không chồng**;
  bỏ qua match gây overlap. Không cần tô mọi lần xuất hiện — match đầu là đủ (có thể tô tất cả nếu dễ).
- **Phân loại màu:** entity vs term khác màu nhẹ (vd entity = xanh, term = cam) để phân biệt.
- **Hover/click vào highlight** → hiện id (`entity_id`/`term_id`) + `source_surface`↔`target_surface`.

## 3. Hard rules / không làm

- Best-effort: **không hứa** căn span chính xác mọi trường hợp; miss → fallback chip, không vỡ.
- Read-only: không thêm nút ghi/sửa nào; không gọi API write.
- Không backend, không đổi shape run (S2), không đụng dataset gold.
- Không dùng span offset của dataset cho khung VI (span EN không map sang VI) — chỉ match chuỗi `target_surface`.

## 4. Tiêu chí nghiệm thu (trên một preview run của `gold_demo_01` ch01)

- Block b005 VI `"Khúc Chuyển không chậm đâu," Người Giữ Đồng Hồ nói, tay lật chiếc đồng hồ cát. "Chỉ là cháu đến sớm thôi."`:
  - tô sáng **Khúc Chuyển** (term g_001), **Người Giữ Đồng Hồ** (entity e_002), **đồng hồ cát** (term g_002);
  - "lật" KHÔNG bị tô (không phải surface nào).
- Khung EN b005: tô **Turning**, **the Clockkeeper**, **hourglass**.
- Mention có `target_surface` không xuất hiện trong `target_text` → không tô, chip vẫn còn, UI không lỗi.
- Hover highlight → thấy id + cặp surface.
- **Không** có đường ghi dữ liệu nào; backend tests vẫn OK (nếu thuần frontend thì không ảnh hưởng).

## 5. Ngoài phạm vi
- Mọi ghi vào dataset gold/canonical.
- Sửa/căn span của annotation dataset (đây chỉ là highlight trên bản preview).

## 6. Tham chiếu
- `app/prototype/parts_center.jsx` (`TranslationPreviewView`, S3) + `styles.css`.
- `skills/dataset-translation-preview/references/TRANSLATION_PREVIEW_CONTRACT.md` (`mentions.target_surface` phải xuất hiện trong `target_text`).
- `tasks/TASK_S3_translation_preview_view.md`.

## 7. Implementation status
- Status: implemented in `app/prototype` as best-effort read-only highlighting inside S3 Preview mode.
- Matching: uses `mentions[].source_surface` for EN and `mentions[].target_surface` for VI; longest non-overlapping matches win.
- Fallback: missing surfaces are not highlighted and do not raise an error; block-level chips remain visible.
- Backend/storage: no new endpoint and no write path; preview remains `working/translation_preview/` only.
- Verification: run backend tests plus browser smoke on a preview run with entity/term mentions.
