# SPEC - Source Structure Normalizer

**Mã đề xuất:** `source-structure-normalizer`

**Scope:** AI-LAB / AIL-202 - bước hỗ trợ **trước extractor**, dùng cho cả **TXT và EPUB**. Nhiệm vụ duy nhất là nhận diện đúng **body thật / chương / tiêu đề chương / ranh giới đoạn / front-back matter** của nguồn để extractor build `document.json` đúng cấu trúc. **Không đổi dataset schema hiện tại (1.5.0).**

**Pipeline tham chiếu:** `extraction.py` từ `0.3.3` trở lên.

## 1. Mục Tiêu Và Nguyên Tắc

Extractor không thể hoàn hảo cho mọi TXT/EPUB lộn xộn. Normalizer thêm một lớp Agent hỗ trợ cấu trúc, nhưng giữ ranh giới cứng:

> Agent **không viết lại nội dung** và **không gắn tag/metadata**. Agent chỉ trả về `StructurePlan` tham chiếu bằng `part_index`/`spine_index`: bỏ phần nào, part nào là tiêu đề chương, part nào cần gộp, item EPUB nào là body/front/back matter, và part nào cần người xem. Code áp dụng plan tất định, đối chiếu lại raw source, rồi sinh `normalized_document.json` làm đầu vào cho extractor.

Ba nguyên tắc bất biến:

1. **Raw source bất biến:** LLM không được âm thầm đổi chữ.
2. **Không giao LLM đếm offset:** mọi span/offset do code hoặc UI text-selection xử lý.
3. **Human-in-the-loop:** AI chỉ tạo nháp cấu trúc; người duyệt và validator chốt ở khâu sau.

## 2. Phạm Vi

Agent **được** quyết định, chỉ về cấu trúc:

- Part/spine item nào là **front/back matter cần loại**: title page, copyright, Gutenberg license, TOC lặp, imprint, colophon, uncopyright.
- Part nào là **tiêu đề chương** và title chuẩn hóa nhẹ của chương.
- Các part nào cần **gộp thành một đoạn** vì bị hard-wrap/tách dòng lạ.
- Part nào **nghi vấn cần người xem**: joined paragraphs, heading mơ hồ, TOC lệch.
- Với EPUB: spine/nav/ncx item nào thuộc body, item nào là front/back matter.

Agent **không** được:

- Trả về nội dung văn bản mới hoặc viết lại chữ.
- Gắn glossary, entity, summary, reference, discourse, hoặc `block_type`.
- Tự tách một part thành nhiều đoạn bằng offset.
- Sửa field read-only hoặc ghi thẳng vào canonical/freeze.

> `block_type` (`heading`/`paragraph`/`dialogue`/`footnote`) vẫn là việc của extractor + người review. Normalizer chỉ lo chương/đoạn/front-back matter.

## 3. Pipeline

```text
raw TXT/EPUB bất biến
  -> deterministic parser tạo candidate parts
  -> Agent đọc candidate parts + tín hiệu TOC/nav/ncx
  -> Agent trả StructurePlan bằng index
  -> code validate plan
  -> code sinh normalized_document.json
  -> extractor build document.json
  -> human review trên web tool -> Validate -> Freeze
```

Với **EPUB**, code phải ưu tiên OPF/nav/ncx trước; Agent chỉ bật khi extractor report cho thấy cấu trúc có vấn đề. Với **TXT**, Agent có thể hữu ích hơn vì TXT thiếu cấu trúc máy đọc được.

## 4. File Working

```text
raw/source.txt | raw/source.epub              # bất biến
working/normalized/structure_plan.json        # output Agent đã validate
working/normalized/normalization_report.json  # audit drop/merge/flag/role + provenance AI
working/normalized/normalized_document.json   # nguồn logic chuẩn hóa cho extractor
```

Các file này nằm trong `working/`, không vào canonical/freeze, không đổi schema.

## 5. Đầu Vào Agent

Code cung cấp danh sách `parts` thống nhất cho TXT/EPUB:

```json
{
  "doc_id": "book_01",
  "source_format": "txt",
  "pipeline_version": "0.3.3",
  "source_fingerprint": "<hash: part count + short hash per part>",
  "parts": [
    {
      "index": 0,
      "source_ref": { "line_start": 1, "line_end": 1 },
      "text": "ALICE'S ADVENTURES IN WONDERLAND",
      "n_lines": 1,
      "is_heading_candidate": true
    },
    {
      "index": 1,
      "source_ref": { "line_start": 20, "line_end": 20 },
      "text": "CHAPTER I. Down the Rabbit-Hole",
      "n_lines": 1,
      "is_heading_candidate": true
    }
  ]
}
```

Với EPUB, mỗi part có thể có thêm: `spine_index`, `nav_title`, `doc_type_tokens`, `heading_level`; `source_ref` nên có `href` và `anchor` nếu có.

Ghi chú:

- `text` chỉ để Agent đọc và phán đoán; Agent không được trả lại nó như nội dung mới.
- `index` phải là index toàn cục ổn định.
- `source_ref` chỉ để debug/provenance; Agent không được sửa trường này.

## 6. Đầu Ra Agent: StructurePlan

```json
{
  "doc_id": "book_01",
  "source_fingerprint": "<phải khớp input>",
  "drop_parts": [
    { "part_index": 0, "reason": "title_page" },
    { "part_index": 412, "reason": "gutenberg_license" }
  ],
  "chapter_headings": [
    { "part_index": 1, "title": "Down the Rabbit-Hole" }
  ],
  "merge_parts": [
    { "part_indices": [31, 32], "reason": "hard_wrapped_paragraph", "separator": " " }
  ],
  "epub_section_roles": [
    { "spine_index": 0, "role": "front_matter", "reason": "titlepage" },
    { "spine_index": 4, "role": "body" }
  ],
  "flags": [
    { "part_index": 88, "flag": "needs_human_check", "note": "possible joined paragraphs" }
  ],
  "confidence": 0.0,
  "notes": ""
}
```

Ràng buộc:

- `reason` cho `drop_parts` là enum cho phép (đồng bộ với §2): `title_page | copyright | imprint | gutenberg_license | uncopyright | toc_repeat | colophon | front_matter | back_matter`.
- `merge_parts[].separator` là enum, chỉ nhận `" "` (một dấu cách) hoặc `"\n\n"` (newline kép); Agent không được đưa chuỗi tùy ý. Dùng `" "` cho `hard_wrapped_paragraph` (nối lại một đoạn bị tách dòng); dùng `"\n\n"` khi cần giữ ranh giới đoạn. Nếu thiếu, code mặc định `"\n\n"`. Code từ chối plan nếu `separator` ngoài enum.
- `title` phải **suy ra được từ text heading gốc**, chỉ qua các phép: NFC, chuẩn hóa whitespace, đổi hoa/thường, bỏ dấu câu cuối, và **bỏ nhãn chương dẫn đầu** (vd `Chapter N`, số La Mã, chữ số, kèm dấu `.`/`:`/`—` theo sau). Phần chữ còn lại phải là chuỗi con nguyên văn của heading; **không thêm/bịa từ mới**. Nếu heading chỉ là nhãn (vd `CHAPTER I.`) không có tên, để `title` rỗng/`null` và flag để người đặt tên.
- `confidence` nằm trong `[0, 1]`, chỉ dùng để ưu tiên review. Nếu `confidence < 0.75`, report phải ghi `low_confidence` hoặc `needs_human_check`. `confidence` không được dùng để bỏ qua review/validate/freeze.

## 7. normalized_document.json

`normalized_document.json` là đầu ra của code, không phải của Agent:

```json
{
  "doc_id": "book_01",
  "source_format": "txt",
  "chapters": [
    {
      "order_index": 1,
      "title": "Down the Rabbit-Hole",
      "blocks": [
        { "source_part_refs": [12], "text": "Alice was beginning to get very tired..." }
      ]
    }
  ]
}
```

- `text` do code lắp ráp bằng cách nối nguyên văn text của các part trong `source_part_refs`, dùng đúng `separator` của merge (mặc định `"\n\n"`); text từng part giữ nguyên, không sửa chữ.
- Không có `block_type` ở đây; extractor tự phân loại khi build `document.json`.

## 8. Code Áp Dụng Và Kiểm Chứng

Trước khi dùng plan, code bắt buộc kiểm:

1. **Fingerprint khớp:** nếu parts đã đổi thì từ chối plan.
2. **Index hợp lệ và không xung đột:** mọi `part_index`/`spine_index` tồn tại; một part không vừa `drop` vừa `heading`; `merge_parts[].separator` nằm trong enum cho phép.
3. **Precedence EPUB:** `epub_section_roles` xét trước — spine `front_matter`/`back_matter` loại toàn bộ part thuộc spine đó; chỉ trong spine `body` mới xét tiếp `drop_parts` từng part.
4. **Gán mặc định rõ ràng (không bỏ lặng lẽ):**
   - Part không bị `drop` và không phải `heading` → là body block, gán cho heading gần nhất phía trước.
   - Part trước heading đầu tiên mà **không** nằm trong `drop_parts` → không xóa, gắn `needs_human_check`.
   - Nếu `chapter_headings` rỗng → fallback một chương duy nhất và gắn `needs_human_check` cho cả doc; không để code đứng hình.
5. **Bất biến nội dung:** với mỗi block, `text == separator.join(text của các part trong refs)` (separator theo §6); text từng part không đổi. Nếu lệch thì abort.
6. **Drop guard (chống mất nội dung):**
   - Audit ghi kèm **snippet text** của mỗi part bị drop, không chỉ index + reason.
   - Nếu tổng ký tự bị drop vượt `DROP_FRACTION_WARNING_THRESHOLD = 0.30` (30% tổng ký tự nguồn) → tự gắn `low_confidence` + `needs_human_check`, không áp lặng lẽ.
7. **Title validation:** title phải suy ra được từ heading part theo các phép cho phép ở §6 (gồm bỏ nhãn chương dẫn đầu); nếu lệch ngoài tập đó (thêm/bịa từ mới) thì bỏ title, dùng text heading gốc và flag.
8. **Audit:** mọi drop/merge/flag/role + provenance AI ghi vào `working/normalized/normalization_report.json`.

Plan của Agent là lớp bổ sung cho deterministic extractor (`clean_gutenberg_text`, OPF/nav/ncx...), không thay thế chúng.

## 9. Trường Hợp Cần Tách Đoạn

Nếu một part chứa nhiều đoạn dính nhau và cần cắt theo offset, Agent không tự cắt. Agent chỉ gắn `needs_human_check`. Người làm tách/sửa thủ công trên web tool; code tính lại span.

## 10. Trigger

- **TXT:** match_rate TOC thấp, không có TOC, `low_confidence`, nhiều heading nghi ngờ.
- **EPUB:** nav lẫn front/back matter với body, nhiều chương trong một HTML file có anchor, title page/contents/colophon bị nhầm thành chương, heading HTML không rõ cấp.
- EPUB đã được extractor report là sạch (`PASS`, không `low_confidence`, không leak front/back matter, không thiếu chương) thì không cần Agent.

## 11. Human Review, Provenance, Validator

Sau khi extractor build `document.json` từ normalized source, quy trình web không đổi:

```text
review clean_text/block_type
  -> Validate
  -> Freeze
```

Provenance AI ghi ở working artifact:

```json
{ "assist_model": "model-name", "prompt_id": "source-structure-normalizer-v1", "assisted_at": "..." }
```

Thông tin này nằm trong `working/normalized/normalization_report.json`, không vào canonical/freeze.

## 12. Tiêu Chí Nghiệm Thu

- Trên nguồn khó, sau khi áp plan: chương/đoạn khớp hơn, front/back matter bị loại đúng, không còn heading lệch nghiêm trọng.
- `normalized_document.json` giữ nguyên chữ bằng phép nối các part nguồn.
- Fingerprint cũ bị từ chối.
- Index xung đột bị từ chối.
- Title bịa/lệch quá mức bị bỏ và flag.
- Part `needs_human_check` hiện cờ cho người xử lý.
- StructurePlan không chứa glossary/entity/summary/reference/discourse/block_type.
- Toàn bộ drop/merge/flag/role có audit report, kèm snippet text của part bị drop.
- Part trước heading đầu (không drop) bị gắn `needs_human_check`, không bị xóa lặng lẽ.
- `chapter_headings` rỗng → fallback một chương + flag toàn doc.
- Drop vượt ngưỡng → tự gắn `low_confidence` + `needs_human_check`.
- Spine `front/back_matter` được loại trước `drop_parts` (precedence EPUB).
- Unit test: fingerprint mismatch, index conflict, separator-enum enforcement, content invariance theo separator, title validation, merge giữ nguyên text, drop-fraction guard, empty-headings fallback, EPUB role precedence.

## 13. Ngoài Phạm Vi

- Không OCR, không layout PDF, không xử lý `formula`/`table_cell`.
- Không annotate: không glossary, entity, summary, reference, discourse, block_type.
- Không cho Agent ghi canonical/freeze.
- Không thuộc hệ thống dịch tự động.

## Chốt

Normalizer chỉ làm một việc: đưa nguồn TXT/EPUB lộn xộn về một cấu trúc chương/đoạn sạch để extractor làm tiếp. Agent gợi ý bằng index, code áp dụng tất định và giữ nguyên nội dung, người duyệt chốt:

```text
raw bất biến
  -> Agent gợi ý StructurePlan
  -> code áp dụng tất định -> normalized_document.json
  -> extractor -> document.json
  -> human review -> validate -> freeze
```
