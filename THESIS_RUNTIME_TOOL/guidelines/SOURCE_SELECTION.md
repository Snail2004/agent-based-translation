# Source Selection Guideline

## Mục tiêu

Chọn nguồn tiếng Anh phù hợp để xây dataset văn bản dài Anh-Việt.

## Ưu tiên

1. Tác phẩm văn học/truyện/tiểu thuyết tiếng Anh có cấu trúc chương rõ.
2. Có định dạng dễ xử lý: `txt`, `epub`, hoặc HTML sạch.
3. License rõ: public domain, CC0, hoặc giấy phép cho phép dùng nghiên cứu.
4. Không quá dài ở MVP: chọn vài chương trước để chạy end-to-end.
5. Nội dung có dialogue, nhân vật, tên riêng, motif hoặc cách xưng hô để annotation có giá trị.

## Nên tránh

- Nguồn không rõ license.
- PDF scan hoặc file cần OCR.
- PDF layout phức tạp, nhiều bảng/hình/công thức.
- Văn bản quá nổi tiếng nếu không ghi rõ contamination risk.
- Bản dịch tiếng Việt đã xuất bản dùng làm gold chính.

## Sheet shortlist nên có

| Field | Mô tả |
|---|---|
| Người đề xuất | Thành viên đề xuất nguồn |
| Tên sách/tác phẩm | Title |
| Link nguồn | URL |
| Format | txt / epub / html / pdf |
| Số chương/độ dài | Ước lượng |
| Thể loại | fantasy, mystery, children's lit, gothic... |
| License note | Public domain / CC / cần verify |
| Contamination risk | low / medium / high |
| Ghi chú | Đã đọc chưa, khó/dễ, lý do chọn |

## Definition of done cho nguồn

- Có URL/provenance.
- Có license note.
- Có format tải được.
- Có sha256 raw file sau khi tải.
- Có quyết định chọn/không chọn.

