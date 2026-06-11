# AI-LAB Handoff Package — AIL-202

Thư mục này là gói tài liệu riêng để giao cho nhóm AI-LAB. Người nhận chỉ cần đọc và làm theo các file trong thư mục này.

## PROVENANCE

Thư mục này được vendor từ `AILAB_HANDOFF` ngày 2026-06-11 để làm nền cho `THESIS_RUNTIME_TOOL`. Từ thời điểm này bản copy phục vụ khóa luận tiến hóa độc lập; tài liệu AI-LAB còn lại chỉ là tham khảo donor, không phải chỉ thị thesis.

## Mục Tiêu

Nhóm thực hiện nhiệm vụ AIL-202:

- xây dựng bộ dữ liệu dịch Anh-Việt cho văn bản dài, ưu tiên truyện/tiểu thuyết/văn học;
- áp dụng quy trình doanh nghiệp: chọn nguồn, kiểm license, trích xuất, chuẩn hóa JSON, annotation, review, validation, versioning, báo cáo;
- xây web tool nội bộ nếu cần để hỗ trợ thao tác dataset.

Dataset cần được thiết kế đủ sạch để có thể tái sử dụng cho các báo cáo, thử nghiệm hoặc bước mở rộng sau này. AI-LAB chịu trách nhiệm phần dataset và quy trình tạo dataset.

## Ngoài Phạm Vi

Nhóm AI-LAB không làm:

- hệ thống dịch tự động hoặc multi-agent;
- app dịch tự động toàn bộ tác phẩm;
- OCR;
- bảo toàn layout PDF;
- pipeline ODL/YOLO/PyMuPDF để render lại PDF;
- benchmark/evaluation suite nâng cao ngoài phạm vi AIL-202.

## Đọc Theo Thứ Tự

1. `AILAB_PLAN.md`
   - Phạm vi AI-LAB.
   - Work packages cho 2 nhánh Data/Language và Web/Tooling.
   - Timeline theo 4 phase.
   - Deliverables, Definition of Done, cut-line, risks.

2. `WORKFLOW.md`
   - Quy trình thao tác cho mỗi nguồn.
   - Field nào code sinh, field nào annotator sửa, field nào reviewer/lead duyệt.
   - Vòng đời reference VI: draft trong working log, reviewed/locked mới vào JSONL.
   - Chính sách dùng AI và các lớp chặn lỗi.

3. `dataset_spec/sample/gold_demo_01/README.md`
   - Golden sample để hiểu format thật.
   - Xem cách document/glossary/entities/summary/reference liên kết với nhau.

4. `guidelines/`
   - `SOURCE_SELECTION.md`: tiêu chí chọn sách/truyện/nguồn.
   - `ANNOTATION_GUIDELINE.md`: cách annotate glossary, entity, summary, reference.
   - `QUALITY_CHECKLIST.md`: checklist QC trước freeze.

5. `WEB_TOOL_SPEC.md`
   - Đặc tả web tool nội bộ.
   - Tool chỉ hỗ trợ tạo, sửa, review, validate và export dataset.
   - Tool không phải app dịch.

## Kết Quả Đầu Ra Mong Đợi

### Đầu ra mỗi nguồn

Mỗi nguồn dữ liệu sau khi hoàn thành nên có tối thiểu:

- `document.json`: cấu trúc chapter/block, source_text, clean_text, metadata nguồn, provenance.
- `glossary.jsonl`: term quan trọng, target dự kiến, allowed/forbidden variants, occurrence spans.
- `entities.jsonl`: nhân vật/địa danh, alias, pronoun policy, mention spans.
- `chapter_summaries.jsonl`: tóm tắt chương tối thiểu `summary_source` + `source`.
- `manual_reference_subset.jsonl`: các đoạn dịch VI đã reviewed/locked.

Reference VI là capacity-based: bắt đầu bằng một tập reviewed tối thiểu để chứng minh quy trình, sau đó dịch và verify được càng nhiều càng tốt. Không bắt buộc parallel VI toàn bộ nếu không đủ năng lực.

### Đầu ra cấp nhóm

- Dataset JSON validate pass.
- Tài liệu chọn nguồn và license/provenance.
- Annotation guideline và QC checklist.
- IAA/review report nếu có overlap annotation.
- Version/freeze log hoặc changelog.
- Web tool demo được luồng dataset browser / edit / review / validate / export.
- Báo cáo mô tả quy trình doanh nghiệp đã áp dụng.

## Quy Trình Tổng Quan

```text
Chọn nguồn + kiểm license
  -> Extract bằng pipeline Python
  -> Tạo JSON draft theo schema
  -> Review/sửa clean_text và metadata
  -> Annotate glossary/entities/chapter summaries/reference VI
  -> Review chéo và QC
  -> validate.py PASS
  -> Freeze version + báo cáo
```

## Cấu Trúc Gói Handoff

```text
AILAB_HANDOFF/
  AILAB_PLAN.md
  WORKFLOW.md
  WEB_TOOL_SPEC.md
  README.md

  dataset_spec/
    schema/       # JSON Schema 1.5.0
    tools/        # validate.py + requirements
    sample/       # golden sample validate pass
    templates/    # working log templates

  guidelines/
    SOURCE_SELECTION.md
    ANNOTATION_GUIDELINE.md
    QUALITY_CHECKLIST.md

  project_template/
    raw/
    canonical/
    working/
    logs/
    exports/

  app/
    README.md
    WEB_TOOL_SPEC.md
```

## Validator

Chạy kiểm tra golden sample:

```powershell
pip install -r dataset_spec/tools/requirements.txt
python dataset_spec/tools/validate.py --dataset dataset_spec/sample/gold_demo_01 --schema dataset_spec/schema
```

Kỳ vọng: `RESULT: PASS`.

## Quy Tắc Ngắn

- `source_text`, id, order, provenance là read-only.
- Span/offset do code hoặc UI selection tính, không giao LLM đếm.
- Glossary/entity là ràng buộc nhất quán, không phải lệnh replace-all.
- Raw AI output không được coi là gold reference.
- Chỉ dữ liệu đã reviewed/locked mới vào freeze.
- Freeze chỉ hợp lệ khi validate pass và QC/review đủ.

## Cách Dùng Với Nhóm

Gửi toàn bộ thư mục `AILAB_HANDOFF/` cho nhóm. Khi cần làm project thật, copy `project_template/` thành một thư mục nguồn mới, đặt raw file vào `raw/`, rồi làm theo `WORKFLOW.md`.
