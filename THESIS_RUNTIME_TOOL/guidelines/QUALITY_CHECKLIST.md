# Quality Checklist

## Trước khi freeze một nguồn

### Provenance/license

- [ ] Có `source_url` hoặc mô tả nguồn.
- [ ] Có `license` và ghi chú license.
- [ ] Có `raw_sha256`.
- [ ] Có `source_format`.
- [ ] Có `contamination_risk`.

### Document structure

- [ ] `document.json` validate pass.
- [ ] Có ít nhất 1 chapter.
- [ ] Mỗi chapter có block.
- [ ] Không có block rỗng.
- [ ] `block_type` chỉ là `heading`, `paragraph`, `dialogue`, `footnote`.
- [ ] `source_text` read-only, không bị thay bằng bản dịch.
- [ ] `clean_text` đã review với các block chính.

### Glossary/entity

- [ ] Glossary term có occurrence hợp lệ.
- [ ] Entity có mention hợp lệ.
- [ ] Không có dangling ref.
- [ ] `canonical_target`, variants, `pronoun_policy` đã được review.
- [ ] Không dùng glossary như replace-all từ văn phong thường.

### Summary/reference

- [ ] Chapter summary có `summary_source` + `source`.
- [ ] Reference subset nếu có thì mọi dòng có `source` + `status`.
- [ ] Reference `status` chỉ là `reviewed` hoặc `locked`.
- [ ] AI involvement được ghi rõ nếu có.

### Validation/QC

- [ ] Chạy `validate.py` pass.
- [ ] Review chéo các phần quan trọng.
- [ ] Có `review_state.json`.
- [ ] Có changelog/version cho freeze.
- [ ] Không đưa draft chưa review vào canonical.

## Lỗi thường gặp

- Sửa `clean_text` làm lệch span nhưng không re-tag.
- Xóa entity/term nhưng block vẫn trỏ id cũ.
- Ghi draft reference thẳng vào JSONL.
- Ghi nhầm progress UI vào schema canonical.
- Dùng `canonical_target` như bắt buộc replace-all.
- Trộn tài liệu PDF layout vào schema văn học.

