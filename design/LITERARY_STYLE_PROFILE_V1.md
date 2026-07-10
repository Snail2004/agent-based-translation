# LITERARY STYLE PROFILE V1 — style dịch là lựa chọn tuyên bố trước, không phải ngẫu nhiên của model

Chốt với user 2026-07-10. Nguyên tắc lõi: **Story Bible ghi SỰ KIỆN (style-free, build một lần);
STYLE PROFILE ghi LỰA CHỌN RENDER (khoá trước khi dịch, ghi provenance).** Hai trục trực giao —
cùng một Story Bible + hai profile → hai bản dịch khác nhau nhưng mỗi bản tự nhất quán.

## 1. Phạm vi ảnh hưởng (để biết khi nào PHẢI khoá — tránh chạy lại)
- **KHÔNG ảnh hưởng:** M1 (B0-B2), M2 (B3), M3 (B4) — Builder trích sự kiện tiếng Anh, bất biến
  theo style. Mọi artifact/checkpoint Builder giữ nguyên giá trị khi đổi style.
- **ẢNH HƯỞNG — khoá profile TRƯỚC khi chạy lần đầu:**
  1. Bước LLM chốt address-policy VN (input = vocative evidence B4 + profile → bảng xưng hô
     cặp-theo-pha). ← **NHẮC: đây là mốc khoá SỚM NHẤT.**
  2. Translator prompt (khối STYLE_PROFILE trong system prompt — global, cacheable prefix).
  3. Đo lường: arm ĐƯỢC ĐO phải dùng profile khớp dịch giả tham chiếu (Dương Tường), nếu không
     điểm reference-based lẫn style-difference với error (vết gold-is-style-guide ở D2L).
     Profile khác = arm demo, không so điểm với reference.

## 2. Cơ chế (spec cho CodeX khi tới lúc — theo pattern prompt-loader hiện có)
- Profile sống trong doc này dưới dạng BLOCKQUOTE có version marker
  (`literary_style_profile_<id>_v<n>`), load bằng đúng `load_system_prompt_from_design` — chỉ
  blockquote tới được model. Đổi profile = version mới, KHÔNG sửa đè.
- Config translator thêm `style_profile_version`; run provenance ghi id này như ghi model-per-stage.
- Phân biệt: extraction prompt phải book-neutral (luật cũ); style profile là artifact PER-BOOK/
  PER-PROJECT — nhắc tên Joseph/WH trong profile là hợp lệ.

## 3. Các chiều lựa chọn của một profile (schema nội dung)
Mỗi chiều = 1 quyết định + 1-2 ví dụ ngắn. Giá trị cho profile Dương Tường: điền sau DESK-CHECK
trên bản dịch tham chiếu (task §5) — KHÔNG bịa từ trí nhớ.
1. **Xưng hô theo quan hệ + pha** (lõi): quy ước cặp (người lạ trưởng thành lịch sự; chủ–người ở;
   gia đình; vợ chồng; với trẻ con), cách xử "sir/madam", đổi xưng hô khi pha quan hệ đổi
   (Story Bible cho biết KHI NÀO đổi; profile cho biết ĐỔI THÀNH GÌ).
2. **Giọng người kể**: Lockwood-tôi (thượng lưu, mỉa) vs Nelly-tôi (bình dân, ấm) — khác register
   thế nào trong tiếng Việt.
3. **Dialect/idiolect** (Joseph): dịch thành giọng quê generic / một phương ngữ VN cụ thể / chuẩn
   hoá + giữ dấu hiệu từ vựng? (TO-VERIFY với bản tham chiếu.)
4. **Tên riêng & địa danh**: giữ nguyên (Heathcliff) vs dịch nghĩa (Wuthering Heights → "Đồi Gió Hú"
   trong văn?) vs phiên âm. (TO-VERIFY.)
5. **Danh xưng Mr./Mrs./Miss**: ông/bà/cô + dạng tên nào; "Mrs. Dean" → "bà Dean"? (TO-VERIFY.)
6. **Nhịp câu & dấu**: giữ câu dài kiểu Brontë hay ngắt; xử em-dash/chấm phẩy. (TO-VERIFY xu hướng.)
7. **Thành ngữ & mức Việt hoá**: dịch sát hình ảnh gốc vs thay thành ngữ Việt tương đương.
8. **Thán từ/cảm thán**: hệ "ôi/chao/trời đất" dùng đến đâu.

## 4. BLOCKQUOTE PROFILE (v1 — KHUNG, các giá trị TO-VERIFY sẽ điền sau desk-check §5)
> [literary_style_profile_duongtuong_v1] — DRAFT, chưa khoá. Sẽ hoàn thiện sau desk-check;
> không inject bản draft này vào bất kỳ run đo nào.

## 5. Việc còn lại trước mốc khoá (Claude chủ trì — phán đoán ngôn ngữ là việc của Claude)
- DESK-CHECK 0-API trên bản Dương Tường (epub tham chiếu, ngoài git): lấy 3-4 đoạn định vị —
  (a) Lockwood↔Heathcliff ch1 (sir, tôi–ông/ngài?), (b) một đoạn Joseph ch2/ch9 (dialect),
  (c) đoạn Nelly bắt đầu kể ch4 (giọng người kể), (d) cách viết tên nhà/địa danh — điền giá trị
  các chiều §3, chốt blockquote v1, user duyệt → LOCK.
- CodeX (sau lock): wiring loader + config + provenance theo §2, 0-API test: profile xuất hiện
  trong prompt render đúng version, đổi version → prompt đổi.

## NHẮC LỊCH (Claude tự nhắc user)
- Trước khi chạy **bước address-policy VN đầu tiên** hoặc **Translator pilot WH đầu tiên**:
  dừng, hoàn thiện §5, khoá profile. M2/M3 hiện tại KHÔNG bị chặn.
