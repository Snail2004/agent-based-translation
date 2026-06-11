# Annotation Guideline

## Nguyên tắc chung

Annotation phục vụ consistency và chất lượng văn học, không phải layout PDF.

- Chỉ annotate thứ có ích cho dịch.
- Không bịa metadata nếu không có evidence.
- Không đưa từ văn phong thường vào glossary.
- Entity/glossary là consistency memory, không phải replace-all.
- Nếu không chắc, để trống hoặc đánh dấu `needs_review`.

## Block types

Schema 1.5.0 chỉ dùng 4 loại:

| Type | Khi dùng |
|---|---|
| `heading` | Tiêu đề chương/section |
| `paragraph` | Đoạn văn kể chuyện hoặc mô tả |
| `dialogue` | Đoạn thoại rõ người nói hoặc câu thoại |
| `footnote` | Chú thích thật sự thuộc nội dung nguồn |

Không dùng `formula`, `table_cell`, `list_item` trong hướng văn học.

## Glossary

Annotate glossary khi term/cụm từ:

- lặp lại nhiều lần;
- là named concept trong thế giới truyện;
- là tên vật/địa danh/khái niệm cần dịch ổn định;
- nếu dịch sai sẽ làm mất mạch hoặc sai nghĩa.

Không annotate:

- tính từ/trạng từ thông thường;
- động từ phổ biến;
- từ tạo phong cách như `curious`, `strange`, `quietly`;
- cụm chỉ xuất hiện một lần và không quan trọng.

Field quan trọng:

- `source_term`
- `expected_target`
- `allowed_variants`
- `forbidden_variants`
- `status`
- `occurrences`

## Entity

Annotate entity khi là:

- nhân vật;
- địa danh;
- tổ chức;
- vật thể/khái niệm có vai trò narrative.

Field quan trọng:

- `entity_type`: schema hiện nhận `person`, `place`, `org`, `concept`. Với nhân vật văn học, kể cả nhân vật động vật có vai trò nhân vật, dùng `person` để validation pass.
- `canonical_source`
- `canonical_target`
- `aliases_source`
- `aliases_target`
- `pronoun_policy`
- `mentions`

Entity không ép mọi mention phải dùng `canonical_target`. Alias, đại từ, hoặc lược chủ ngữ tự nhiên vẫn hợp lệ nếu không sai danh tính.

## Entity relations (quan hệ nhân vật & xưng hô)

Lưu ở sidecar `entity_relations.jsonl` (1.5.0). Mỗi dòng là một **quan hệ có hướng giữa hai entity**, chủ yếu để chọn **xưng hô tiếng Việt** đúng trong lời thoại.

Chỉ tạo relation khi:

- có **evidence** trong văn bản (cách hai nhân vật gọi nhau, mô tả quan hệ);
- quan hệ ảnh hưởng tới xưng hô/dịch (ưu tiên person–person);
- KHÔNG tạo quan hệ cho mọi cặp entity (không N×N).

Field quan trọng:

- `source_entity_id`, `target_entity_id` — quy ước: **source LÀ `relation_type` của target** (vd `relation_type=parent` ⇒ source là cha/mẹ của target).
- `relation_type` — free-text + giá trị gợi ý: `sibling`/`parent`/`child`/`spouse`/`friend`/`master`/`servant`/`mentor`/`stranger`/`rival`/`guardian`…
- `address_policy` — 4 chuỗi, hai chiều độc lập: `source_to_target: {self_term, address_term}` (source tự xưng gì / gọi target là gì) và `target_to_source: {self_term, address_term}`.
- `evidence` — `[{block_id, surface}]`, chống bịa.
- `confidence`, `notes` — optional.

Quan hệ thay đổi theo diễn biến (vd thân→thù): tạo **nhiều dòng cho cùng cặp**, phân biệt bằng `state_label` + `valid_from_block_id` + `valid_to_block_id` (vắng = áp cả tài liệu). So sánh theo thứ tự tài liệu (`order_index`), không so chuỗi `block_id`.

Bùng cảm xúc nhất thời (một câu) KHÔNG tạo relation mới → dùng `discourse.pronoun_hints` + `annotations.tone/narrative_note` ở block đó.

Validator: `source/target_entity_id`, `valid_from/to_block_id`, `evidence[].block_id` phải trỏ tới đối tượng tồn tại; cảnh báo nếu phase chồng lấp cùng cặp.

## Span

`span = [start, end]` là offset ký tự trong `clean_text`, `end` exclusive.

Quy tắc:

- Không giao LLM tự đếm span.
- Dùng code hoặc UI text-selection.
- Nếu sửa `clean_text`, phải re-check span.

## Discourse

Với `dialogue`, nếu rõ thì điền:

- `speaker_entity_id`
- `addressee_entity_id`
- `pronoun_hints`

Nếu không rõ, để `null` hoặc trống; không đoán quá mức.

## Narrative hints

Các field optional:

- `motifs`
- `tone`
- `implicit_meaning`
- `narrative_note`

Chỉ điền khi có evidence trong text. Đây là hint, không phải gold nếu chưa review/adjudicate.

## Chapter summary

Mỗi chương của nguồn MVP nên có:

- `summary_source`
- `source`

Viết ngay sau khi review xong chương để không phải đọc lại từ đầu.

## Reference VI

Reference VI không bắt buộc phải dịch toàn bộ tác phẩm, nhưng nhóm nên dịch/verify được càng nhiều càng tốt.

Bắt đầu bằng một tập tối thiểu stratified để chứng minh quy trình:

- chapter opening;
- dialogue;
- term-heavy/entity-heavy;
- narrative khó;
- random.

Sau mức tối thiểu, tiếp tục dịch/verify thêm block nếu còn thời gian. Dịch tới đâu đưa vào `manual_reference_subset.jsonl` tới đó, miễn mọi entry đều có `source`, `status`, `translated_by/reviewed_by` nếu có.

Raw AI draft không được đưa thẳng vào `manual_reference_subset.jsonl`. Chỉ entry `reviewed` hoặc `locked` mới vào canonical.
