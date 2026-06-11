# TASK - Source Structure Normalizer Phase 0/1

## 0. Mục Tiêu

Implement lõi deterministic cho **Source Structure Normalizer** theo:

- `guidelines/SOURCE_STRUCTURE_NORMALIZER_SPEC.md`
- schema dataset hiện tại `1.4.0`
- pipeline extractor `0.3.3+`

Phase này **không gọi LLM/Agent**. Chỉ dùng **manual StructurePlan fixtures** để chứng minh core hoạt động đúng. Agent sinh plan sẽ là phase sau.

## 1. Scope Và Guardrails

- Làm trong nested repo `research/agent-based-translation/AILAB_HANDOFF`.
- Không đổi schema `1.4.0`.
- Không đổi luồng extract mặc định cho các nguồn đang PASS.
- Normalizer là **opt-in**: chỉ nguồn được trigger mới đi qua normalizer.
- Artifact runtime nằm trong `working/normalized/`.
- Không commit `ailab_projects/` hoặc raw book files.
- Raw source bất biến; code/LLM không viết lại chữ.
- Không gắn glossary/entity/summary/reference/discourse/block_type trong normalizer.
- `block_type` vẫn do extractor hiện có xử lý.
- **Không "đi tắt" bằng cách sửa heuristic extractor** (vd thêm nhận diện số La Mã trần vào `CHAPTER_RE`) để `canterville_ghost` pass. Việc heuristic bỏ sót chính là *động cơ*, không phải chỗ sửa — phải chứng minh cải thiện **qua normalizer + manual plan**, không phải qua việc vá extractor.
- Test chạy trực tiếp trên raw source path (`AILAB_SOURCES_RAW/canterville_ghost/`); **không** tạo/commit project canterville trong `ailab_projects/`. Artifact normalizer ghi vào thư mục working tạm hoặc gitignored.

## 2. Baseline Cần Vượt Qua

Nguồn test chính: `canterville_ghost`.

Raw hiện có trong:

```text
AILAB_SOURCES_RAW/canterville_ghost/source.txt
AILAB_SOURCES_RAW/canterville_ghost/source.epub
AILAB_SOURCES_RAW/canterville_ghost/source.html
```

Tác phẩm thật có khoảng 7 chương, thường là I-VII.

Baseline extractor hiện tại:

- TXT: `split_txt` ra 1 chương `"Chapter 1"`, `toc_source: none`, `fallback_used: true`.
- EPUB: `split_epub` ra 3 chương rác kiểu `WILDE`, `VI`, `VII`, `toc_source: ncx`, `toc_items: 11`, `low_confidence: true`, có `anchor_split_failed`, front matter lọt vào body.

Mục tiêu: manual StructurePlan phải giúp `canterville_ghost` ra cấu trúc chương/đoạn đúng hơn rõ rệt so với direct extractor.

## 3. Deliverables Code

### 3.1 Candidate Parts

Tạo module gợi ý:

```text
app/backend/services/structure_normalizer.py
```

Implement:

```python
build_candidate_parts(source) -> CandidatePartsInput
```

Yêu cầu:

- TXT: tái dùng `_txt_parts()` nếu phù hợp; bổ sung `source_ref.line_start/line_end` best-effort.
- EPUB: tái dùng logic đọc spine/nav/ncx và `BlockHTMLParser` sẵn có; mỗi part có thể kèm `spine_index`, `nav_title`, `doc_type_tokens`, `heading_level`, `source_ref.href/anchor`.
- Mỗi part có:
  - `index` toàn cục ổn định
  - `text`
  - `n_lines`
  - `is_heading_candidate`
- Sinh `source_fingerprint` từ part count + short hash của từng part.

### 3.2 Load Và Validate StructurePlan

Implement:

```python
load_plan(path) -> StructurePlan
validate_plan(parts, plan) -> validation_result
```

Validate đủ:

- fingerprint match
- `part_index`/`spine_index` tồn tại
- không part nào vừa `drop` vừa `heading`
- `merge_parts[].separator` chỉ nhận `" "` hoặc `"\n\n"`
- `drop_parts[].reason` nằm trong enum cho phép
- title validation: title chỉ là biến thể chuẩn hóa nhẹ của text heading gốc
- EPUB precedence: `epub_section_roles` xét trước, spine `front_matter/back_matter` loại toàn spine; trong spine `body` mới xét `drop_parts`
- drop fraction guard: `DROP_FRACTION_WARNING_THRESHOLD = 0.30`

Default behavior:

- Part không drop và không heading -> body block của heading gần nhất phía trước.
- Part trước heading đầu tiên mà không drop -> không xóa, gắn `needs_human_check`.
- `chapter_headings` rỗng -> fallback 1 chương + flag toàn doc.

### 3.3 Apply StructurePlan

Implement:

```python
apply_plan(parts, plan, output_dir)
```

Ghi:

```text
working/normalized/structure_plan.json
working/normalized/normalized_document.json
working/normalized/normalization_report.json
```

`normalized_document.json` tối thiểu:

```json
{
  "doc_id": "book_01",
  "source_format": "txt",
  "chapters": [
    {
      "order_index": 1,
      "title": "Chapter title",
      "blocks": [
        {
          "source_part_refs": [12],
          "text": "verbatim text"
        }
      ]
    }
  ]
}
```

Content invariance bắt buộc:

```text
block.text == separator.join(part.text for part in source_part_refs)
```

Nếu lệch -> abort, không ghi output.

`normalization_report.json` phải có:

- drop/merge/flag/role audit
- snippet text của mỗi part bị drop
- drop fraction
- low_confidence / needs_human_check nếu có
- provenance AI placeholder nếu sau này dùng Agent

### 3.4 Bridge Vào Extractor

Implement adapter:

```text
normalized_document.json -> chapters_raw -> chapters_to_document()
```

Yêu cầu:

- Reuse `block_type_for()` hiện có trên text từng block.
- Không viết lại logic block_type.
- Không thay default extraction path.
- Chỉ chạy khi explicitly dùng normalizer.

## 4. Tests

Đặt test trong:

```text
app/backend/tests/
```

### 4.1 Golden Tests Cho canterville_ghost

Tạo fixture:

- candidate parts snapshot cho TXT
- candidate parts snapshot cho EPUB nếu khả thi
- manual StructurePlan cho TXT
- manual StructurePlan cho EPUB nếu khả thi
- expected `normalized_document.json`

Assertions:

- TXT: direct extractor 1 chương -> normalizer khoảng 7 chương I-VII.
- EPUB: front matter như `WILDE`, illustrator, `1906` bị loại/flag đúng; cấu trúc chương cải thiện rõ.
- content invariance holds.

### 4.2 Invariant Tests

Bắt buộc test:

- no-op/identity plan không làm mất nội dung
- fingerprint mismatch bị reject
- index conflict bị reject
- invalid separator bị reject
- drop fraction > 0.30 tự gắn `low_confidence` + `needs_human_check`
- part trước heading đầu không drop -> `needs_human_check`, không xóa
- empty `chapter_headings` -> fallback 1 chương + flag toàn doc
- EPUB role precedence nếu feasible

## 5. Báo Cáo Before/After

Tạo report ngắn:

```text
app/reports/SOURCE_STRUCTURE_NORMALIZER_PHASE01.md
```

Nội dung:

- direct extractor result trên `canterville_ghost`
- normalizer + extractor result
- cải thiện gì
- nguồn nào chưa xử lý được
- risks/deferred work
- lệnh verify

## 6. Verification

Chạy:

```powershell
python dataset_spec\tools\validate.py --dataset dataset_spec\sample\gold_demo_01 --schema dataset_spec\schema
python -m unittest discover app\backend\tests
```

Nếu có script/report riêng cho normalizer, ghi command vào report.

## 7. Definition Of Done

- Candidate parts deterministic.
- Manual StructurePlan validate/apply được.
- `normalized_document.json` và `normalization_report.json` sinh đúng.
- `canterville_ghost` cải thiện rõ so với direct extractor.
- No-op plan không làm mất nội dung.
- Existing tests pass.
- Default extraction behavior không đổi.
- Không đổi schema.
- Không commit runtime/raw data trong `ailab_projects/`.
- Commit khi hoàn tất, không push nếu user chưa yêu cầu.

## 8. Phụ Lục - Manual Fixture Cho canterville_ghost

Phụ lục này cung cấp fixture thật cho `canterville_ghost` để tránh Phase 0 phải đoán chỉ số part.

### 8.1 TXT Plan Hoàn Chỉnh

`build_candidate_parts(source.txt)` kỳ vọng có 151 parts.

Bản đồ part:

| Parts | Vai trò | reason |
|---|---|---|
| 0 | PG note: source also has HTML version | `front_matter` |
| 1-5 | title page: `THE CANTERVILLE GHOST`, `by`, `WILDE`, chronicle, illustrator | `title_page` |
| 6-7 | imprint / `1906` | `imprint` |
| 8-25 | `LIST OF ILLUSTRATIONS` + caption list | `front_matter` |
| 26, 49, 56, 69, 80, 117, 132 | Roman numerals I-VII | chapter headings |
| 27-48, 50-55, 57-68, 70-79, 81-116, 118-131, 133-150 | body từng chương | keep |

Manual StructurePlan fixture:

```json
{
  "doc_id": "canterville_ghost",
  "source_fingerprint": "<fill from build_candidate_parts(source.txt)>",
  "drop_parts": [
    { "part_index": 0, "reason": "front_matter" },
    { "part_index": 1, "reason": "title_page" },
    { "part_index": 2, "reason": "title_page" },
    { "part_index": 3, "reason": "title_page" },
    { "part_index": 4, "reason": "title_page" },
    { "part_index": 5, "reason": "title_page" },
    { "part_index": 6, "reason": "imprint" },
    { "part_index": 7, "reason": "imprint" },
    { "part_index": 8, "reason": "front_matter" },
    { "part_index": 9, "reason": "front_matter" },
    { "part_index": 10, "reason": "front_matter" },
    { "part_index": 11, "reason": "front_matter" },
    { "part_index": 12, "reason": "front_matter" },
    { "part_index": 13, "reason": "front_matter" },
    { "part_index": 14, "reason": "front_matter" },
    { "part_index": 15, "reason": "front_matter" },
    { "part_index": 16, "reason": "front_matter" },
    { "part_index": 17, "reason": "front_matter" },
    { "part_index": 18, "reason": "front_matter" },
    { "part_index": 19, "reason": "front_matter" },
    { "part_index": 20, "reason": "front_matter" },
    { "part_index": 21, "reason": "front_matter" },
    { "part_index": 22, "reason": "front_matter" },
    { "part_index": 23, "reason": "front_matter" },
    { "part_index": 24, "reason": "front_matter" },
    { "part_index": 25, "reason": "front_matter" }
  ],
  "chapter_headings": [
    { "part_index": 26, "title": "I" },
    { "part_index": 49, "title": "II" },
    { "part_index": 56, "title": "III" },
    { "part_index": 69, "title": "IV" },
    { "part_index": 80, "title": "V" },
    { "part_index": 117, "title": "VI" },
    { "part_index": 132, "title": "VII" }
  ],
  "merge_parts": [],
  "epub_section_roles": [],
  "flags": [],
  "confidence": 0.9,
  "notes": "Manual fixture for Phase 0. Chapters are bare Roman numerals the heuristic misses; title is the numeral verbatim."
}
```

Expected assertions:

- 7 chapters with titles `I` through `VII`.
- Body part ranges:
  - I: `27-48`
  - II: `50-55`
  - III: `57-68`
  - IV: `70-79`
  - V: `81-116`
  - VI: `118-131`
  - VII: `133-150`
- Content invariance: each block text equals its source part text because no merge is used.
- Drop fraction should stay below `DROP_FRACTION_WARNING_THRESHOLD = 0.30`; no automatic `low_confidence` expected.
- Baseline improvement: direct TXT extractor `1 chapter -> 7 chapters`.

Ghi chú: vì truyện chỉ dùng số La Mã làm tiêu đề chương, `title = "I"..."VII"` là hợp lệ. Nếu implementation chọn title rỗng/null và chỉ dựa vào `order_index`, cũng chấp nhận được nếu tests được cập nhật nhất quán; nhưng fixture mặc định nên dùng Roman numerals nguyên văn để dễ debug.

### 8.2 EPUB Plan Template

Chưa chốt index cứng cho EPUB vì `build_candidate_parts(source.epub)` chính là một phần việc cần implement trong Phase 0/1.

Mục tiêu EPUB:

- Drop hoặc role `front_matter` cho title page, author `WILDE`, illustrator `Wallace Goldsmith`, `1906`, `LIST OF ILLUSTRATIONS`, captions, boilerplate/colophon nếu có.
- Chapter headings là 7 mốc Roman numerals I-VII.
- Nếu nhiều chương nằm trong cùng một HTML file, plan dùng `part_index`/`anchor` từ candidate builder để tách đúng.
- `epub_section_roles` xử lý spine front/back matter trước; trong spine body mới xét `drop_parts`.

Template:

```json
{
  "doc_id": "canterville_ghost",
  "source_fingerprint": "<fill from build_candidate_parts(source.epub)>",
  "epub_section_roles": [
    { "spine_index": "<title/illustrations spine>", "role": "front_matter", "reason": "titlepage" },
    { "spine_index": "<body spine>", "role": "body" }
  ],
  "drop_parts": [
    { "part_index": "<author/illustrator/1906/illustration-caption part>", "reason": "front_matter" }
  ],
  "chapter_headings": [
    { "part_index": "<I anchor>", "title": "I" },
    { "part_index": "<II anchor>", "title": "II" },
    { "part_index": "<III anchor>", "title": "III" },
    { "part_index": "<IV anchor>", "title": "IV" },
    { "part_index": "<V anchor>", "title": "V" },
    { "part_index": "<VI anchor>", "title": "VI" },
    { "part_index": "<VII anchor>", "title": "VII" }
  ],
  "merge_parts": [],
  "flags": [],
  "confidence": 0.85,
  "notes": "Fill indices after EPUB candidate builder exposes spine_index and anchor parts. Target: 7 chapters I-VII, front matter dropped."
}
```

Expected assertions:

- Front matter snippets like `WILDE`, illustrator, `1906`, and illustration list do not become chapter titles.
- Baseline EPUB `3 noisy chapters -> about 7 clean Roman-numeral chapters`.
- If anchor handling cannot be solved in this phase, report it explicitly as deferred instead of silently passing.
> Tài liệu gốc AI-LAB — chỉ tham khảo, không phải chỉ thị thesis.
