# TASK — S3: App song song EN | VI cho translation preview (BONUS / experimental)

**Loại:** frontend `AILAB_HANDOFF/app/prototype` (read-only viewer). **Người làm:** CodeX. **Trạng thái:** chưa làm.

> **Khung an toàn:** đây là **màn hình XEM read-only**, không phải biến app thành app dịch.
> Nó chỉ **render** preview run đã lưu (S2) cạnh văn bản nguồn. **Không** sửa, không lưu,
> không promote, không ghi `canonical/`/gold. Có banner nói rõ "read-only preview, không phải gold".

## 0. Bối cảnh & vị trí roadmap

- S1 (xong): skill nhả preview JSON per-block.
- S2 (xong): backend lưu run vào `working/translation_preview/` + API
  `POST/GET /api/projects/<doc_id>/translation-preview/runs[/<run_id>]`.
- **S3 (task này):** màn hình **song song trái EN / phải VI**, khớp theo `block_id`, hiện
  `used_context` + `address_applied` mỗi block. **Mức block** (CHƯA căn nhãn span = S4).

## 1. Phạm vi

- Frontend `app/prototype` (React-in-jsx như các `parts_*.jsx` hiện có): thêm **một màn hình/tab
  "Translation Preview" read-only**.
- **Không cần backend mới** — dùng API S2 (list/load run) + document đã có (clean_text per block).
  Nếu muốn 1 helper join EN+VI thì optional, nhưng frontend tự join theo `block_id` là đủ.
- Không đổi schema, không đổi workflow dataset-construction.

## 2. UI

- **Chọn:** chương + một preview run (từ `GET …/translation-preview/runs`).
- **Bố cục 2 cột, khớp theo `block_id` (đúng thứ tự document):**
  - Cột trái = `clean_text` (EN) của block.
  - Cột phải = `target_text` (VI) từ preview run.
  - Mỗi hàng = 1 block; căn theo `block_id` (trục bất biến).
- **Dưới/bên mỗi block VI:** 
  - chips `used_context` (entity/term/relation/chapter ids đã dùng);
  - `address_applied` nếu có (vd "xưng hô: cháu / ông · rel_001").
- **Banner đầu màn hình:** "Bản dịch preview — read-only, KHÔNG phải dữ liệu gold."
- Block có trong document nhưng không có trong run (vd heading không dịch) → vẫn hiện EN, VI để trống/"(chưa dịch)".
- Hiện cảnh báo của run (nếu `warnings` có, vd unknown_used_context) ở mức nhẹ.

## 3. Data binding

- `GET …/translation-preview/runs` → danh sách run (chọn).
- `GET …/translation-preview/runs/<run_id>` → `run.blocks[]` (target_text/mentions/address_applied/used_context).
- Document blocks (đã có trong state app) → EN `clean_text`.
- **Join theo `block_id`**; thiếu bên nào thì hiển thị trống bên đó, không lỗi.

## 4. Hard rules / không làm

- **Read-only tuyệt đối:** không nút save/edit/apply/promote/lock trong màn hình này.
- **Không** ghi `canonical/`, `manual_reference_subset`, `working/drafts.json`.
- **Không căn nhãn theo span** (đó là S4) — `used_context` chỉ hiện dạng chips ở mức block.
- Không gọi LLM trong app (preview JSON do skill bên ngoài tạo, import qua S2).
- Tách rõ khỏi luồng annotate/reference; không lẫn vào freeze/QC.

## 5. Tiêu chí nghiệm thu

- Có một preview run đã lưu (vd import output S1 cho một project) → màn hình hiện **EN | VI
  song song, khớp block**, kèm `used_context` chips + `address_applied` mỗi block.
- Đổi run khác cùng chương → nội dung đổi theo.
- Block không có bản dịch trong run → hiện EN, VI trống, không vỡ UI.
- **Không** có nút/đường ghi dữ liệu nào trong màn hình.
- Không phá luồng/test hiện có (chạy `python -m unittest discover app/backend/tests` vẫn OK nếu có đụng backend; nếu thuần frontend thì không ảnh hưởng test).

## 6. Ngoài phạm vi (task sau)
- **S4:** highlight entity/term trong khung VI theo `target_surface` (best-effort span trong `target_text`); fallback mức block khi không match.
- Mọi ghi vào dataset gold.

## 7. Tham chiếu
- `tasks/TASK_S2_translation_preview_store.md` (API + shape run).
- `app/backend/routes/translation_preview.py`, `services/translation_preview.py` (endpoint S2).
- `app/prototype/parts_*.jsx`, `app.jsx` (mẫu thêm screen/tab + binding state).
- `app/prototype/parts_right.jsx` (mẫu render block + chips/status).

## 8. Implementation status

- Status: implemented in `app/prototype` as a read-only `Preview` center mode.
- Backend: no new endpoint; uses S2 `translation-preview/runs` list/load APIs.
- Storage: no write path from this view; preview runs remain in `working/translation_preview/`.
- Verification: run backend tests and browser smoke before commit.
