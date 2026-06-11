# DATASET DESIGN - Agent-Based Long-Document EN-VI Translation

File này là bản thiết kế dataset chính cho hướng nghiên cứu hiện tại. Bản dataset design do agent khác tạo đã được giữ lại ở `DATASET_DESIGN_AGENT_REFERENCE.md` để tham khảo chi tiết về tooling, schema mẫu và annotation.

## 0. Vai trò của Dataset

Dataset không chỉ là tập văn bản để dịch thử. Trong đề tài này, dataset là **evaluation suite** để kiểm định từng đóng góp của kiến trúc:

- memory/retrieval có cải thiện tính nhất quán không;
- summary memory có giúp dịch các đoạn đầu chương tốt hơn không;
- Narrative Understanding Agent + vector retrieval có giúp bản dịch có giọng kể tự nhiên hơn không;
- CriticAgent có phát hiện lỗi đúng không;
- feedback có cải thiện các đoạn downstream không;
- retrieval layer có lấy đúng ngữ cảnh narrative không.

Nguyên tắc quan trọng:

| Nguyên tắc | Hệ quả thiết kế |
|---|---|
| PDF chỉ là adapter | Dataset chuẩn phải là JSON sạch, không phải PDF |
| Literary translation không lệ thuộc BLEU | D2 dùng MHP/BLP/MQM/Likert là chính; chrF/BLEU chỉ phụ khi có reference subset |
| Ground truth phải độc lập với system output | Term/entity/motif/error/relevance annotation không được sinh từ output S0-S3 |
| Cần đo cả intrinsic và extrinsic | D6 đo retrieval có lấy đúng context; D2 human eval đo bản dịch có tốt hơn không |
| Tái lập được | Mọi file có provenance, license, schema version, changelog |
| Không leakage | Mọi system dịch cùng source blocks; reviewer không biết output thuộc system nào |

Đơn vị trung tâm là `block`: mỗi block có `doc_id`, `chapter_id`, `block_id`, `source_text`, `clean_text`, annotation và provenance. Agents dịch theo block, nhưng memory/retrieval dùng thêm chapter/document context.

---

## 1. Research Questions -> Dataset Requirements

| RQ | Câu hỏi | Dataset cần có | Tín hiệu đo |
|---|---|---|---|
| RQ1 | Memory/retrieval có cải thiện consistency? | D2/D3 + D4 term/entity ground truth | TAR, ECS |
| RQ2 | Chapter summary memory có cải thiện coherence đầu chapter? | D2/D3 có chapter boundary rõ, đánh dấu chapter-opening blocks | TAR/ECS/MQM trên block đầu chapter, S3 vs S3a |
| RQ3 | CriticAgent phát hiện lỗi được không? | D5 injected-error set | Precision, Recall, F1 theo error type |
| RQ4 | Feedback có cải thiện downstream blocks? | D2/D3 + feedback protocol | TAR/ECS/preference trên downstream blocks, S3 vs S3c |
| RQ5 | Narrative Agent + vector retrieval có cải thiện giọng kể? | D2 giàu motif/dialogue + D6 retrieval relevance + human eval | MHP, BLP, Likert narrative, MATTR/MTLD, Recall@K/MRR/NDCG |

Điểm chẩn đoán quan trọng: RQ5 cần **hai bằng chứng riêng**.

1. Intrinsic: D6 cho biết retrieval có lấy đúng context narrative không.
2. Extrinsic: D2 human evaluation cho biết output S3 có tốt hơn S3d không.

Nếu chỉ có extrinsic evaluation, khi S3 không hơn S3d ta không biết lỗi nằm ở retrieval, brief, model dịch hay metric.

---

## 2. Dataset Suite D1-D6

Giữ 6 nhóm dữ liệu thô theo V3, nhưng phân biệt rõ mức bắt buộc.

| Mã | Nhóm dữ liệu | Vai trò | Mức ưu tiên |
|---|---|---|---|
| D1 | Sentence-level sanity set | Kiểm tra hệ không làm giảm chất lượng dịch câu cơ bản; dùng chrF/COMET/BLEU phụ | Nên có |
| D2 | Literary document-level set | Lõi RQ2/RQ5: narrative, dialogue, motif, entity, giọng kể | Bắt buộc |
| D3 | Technical/educational document-level set | Lõi RQ1 trên terminology/formula/domain consistency | Nên có, thu hẹp nếu thiếu thời gian |
| D4 | Term/entity ground-truth set | Gold để tính TAR/ECS | Bắt buộc |
| D5 | Injected-error set | Gold để đo CriticAgent precision/recall | Bắt buộc |
| D6 | Retrieval relevance set | Gold để đo FTS/vector retrieval lấy đúng context | Bắt buộc nhỏ |

Không tạo dataset thô riêng cho MHP/human preference. MHP là **evaluation artifact** sinh từ output của các system trên D2/D3.

Không tạo dataset riêng cho feedback. Feedback là **experiment protocol** chạy trên D2/D3.

Minimum để bảo vệ hướng nghiên cứu:

- D2: literary document subset;
- D4: term/entity ground truth lấy từ D2 và D3 hoặc chỉ D2 nếu MVP;
- D5: injected errors;
- D6: retrieval relevance queries.

D1 và D3 giúp luận văn cân bằng hơn, nhưng không nên để chúng đẩy quá tải scope.

---

## 3. Pipeline Nguồn Thô -> JSON Sạch

Dataset source of truth là JSON đã chuẩn hóa. Raw files chỉ là provenance.

```text
[0] Register raw source
    -> doc_id, license, URL/path, sha256, source_format

[1] Detect format
    -> born-digital PDF | scanned PDF | EPUB | TXT | HTML

[2] Extract text/layout
    -> raw_text + page/offset metadata

[3] Remove layout noise
    -> header/footer/page number/watermark

[4] Detect structure
    -> chapter/section/heading/paragraph/dialogue/footnote

[5] Segment blocks
    -> stable block_id, order_index, page_id, char offsets

[6] Sentence split for annotation only
    -> sent_id inside block, not translation unit

[7] Normalize clean_text
    -> whitespace, hyphenation, quotes, OCR noise flags

[8] Automatic quality check
    -> extraction_error, ocr_suspect, formula_suspect, empty_block

[9] Manual correction
    -> targeted corrections only, raw remains immutable

[10] Freeze dataset version
    -> schema validation, changelog, versions.json
```

Sentence split chỉ phục vụ annotation/span alignment, không phải đơn vị dịch. EN dùng một tokenizer cố định như spaCy hoặc NLTK Punkt; VI/reference subset dùng một tokenizer cố định như underthesea hoặc pyvi. Tokenizer phải ghi vào metadata để MATTR/MTLD và span audit không thay đổi giữa các lần chạy.

### 3.1. Tooling theo nguồn

| Nguồn | Công cụ ưu tiên | Ghi chú |
|---|---|---|
| EPUB | `ebooklib` + `BeautifulSoup` | Ưu tiên hơn PDF nếu có, vì chapter/paragraph sạch hơn |
| TXT | custom parser + encoding detection | Tốt cho Project Gutenberg/Standard Ebooks |
| Born-digital PDF | PyMuPDF hoặc pdfplumber | PyMuPDF nhanh; pdfplumber hữu ích nếu cần table/position |
| Scanned PDF | OCRmyPDF/Tesseract, sau đó xử lý như PDF có text layer | Chỉ dùng khi không có nguồn sạch hơn |
| HTML | `BeautifulSoup` hoặc `trafilatura` | Cần loại nav/ads/boilerplate |

Khuyến nghị:

- D2 literary: ưu tiên EPUB/TXT từ nguồn public domain hoặc CC0, tránh PDF nếu có bản text sạch.
- D3 technical: nếu cần xử lý công thức/table, dùng technical/layout extension riêng; schema literary 1.5.0 không có `formula`/`table_cell`.
- Born-digital prose PDF: PyMuPDF đủ tốt cho extraction nhanh; dùng pdfplumber khi cần table/position ổn định hơn.
- Scanned PDF: chỉ dùng khi không có EPUB/TXT/HTML/PDF sạch hơn; mọi block OCR confidence thấp phải được flag/manual review.

Candidate sources để kiểm license sau, chưa chốt thu thập:

| Nhóm | Candidate source | Lý do đưa vào shortlist |
|---|---|---|
| D1 | FLORES-200, PhoMT, IWSLT EN-VI | Sentence-level sanity/reference metrics |
| D2 | Standard Ebooks, Project Gutenberg, public-domain/CC short stories | Có EPUB/TXT sạch; Standard Ebooks thường đã proofread tốt |
| D3 | OpenStax, LibreTexts, tài liệu kỹ thuật/giáo dục có license rõ | Có chapter/section rõ; nhiều term/formula |

License không ghi cứng ở giai đoạn design. Khi thu thập, mỗi document phải verify lại license theo nguồn chính thức và ghi vào `LICENSE_NOTES.md`.

### 3.2. Mức giữ layout

Đề tài không đánh giá tái dựng PDF. Vì vậy dataset chỉ giữ **logical layout**:

- chapter;
- section;
- paragraph;
- dialogue;
- footnote nếu có nghĩa.

`block_type` trong schema literary là logical content/discourse type, không phải layout class. Core set từ schema 1.5.0 là `heading|paragraph|dialogue|footnote`. Các category như `formula`, `table_cell`, và `list_item` thuộc technical/layout processing và chỉ nên thêm lại bằng version bump nếu một nguồn thật sự cần.

Không cần giữ cột, font, pixel bbox trừ khi dùng để trace extraction.

---

## 4. Folder Structure

```text
dataset/
  README.md
  LICENSE_NOTES.md
  CHANGELOG.md
  versions.json
  schema/
    document.schema.json
    glossary.schema.json
    entity.schema.json
    chapter_summary.schema.json
    injected_error.schema.json
    retrieval_relevance.schema.json
  raw/
    <doc_id>/
      source.*
      source.sha256
      provenance.json
  D1_sentence/
    <dataset_name>/
      source.en.jsonl
      reference.vi.jsonl
      metadata.json
  D2_literary/
    <doc_id>/
      document.json
      annotations/
        narrative.jsonl
        discourse.jsonl
      references/
        manual_reference_subset.jsonl
      chapter_summaries.jsonl
  D3_technical/
    <doc_id>/
      document.json
      annotations/
        formulas.jsonl
  D4_termentity/
    <doc_id>/
      glossary.jsonl
      entities.jsonl
  D5_injected/
    <doc_id>/
      injected_errors.jsonl
  D6_retrieval/
    <doc_id>/
      relevance_queries.jsonl
  eval/
    human_preference/
      mhp_pairs.jsonl
      reviewer_forms/
    feedback_protocol/
      feedback_plan.json
```

---

## 5. Core JSON Schema

### 5.1. `document.json`

```json
{
  "schema_version": "1.5.0",
  "doc_id": "d2_literary_001",
  "metadata": {
    "title": "",
    "author": "",
    "domain": "literature",
    "genre": "fantasy",
    "source_language": "en",
    "target_language": "vi",
    "source_format": "epub|txt|pdf|html",
    "license": "",
    "license_url": "",
    "source_url": "",
    "raw_sha256": "",
    "retrieved_at": "",
    "extraction_tool": "",
    "pipeline_version": "0.1.0",
    "contamination_risk": "low|medium|high"
  },
  "chapters": [
    {
      "chapter_id": "d2_literary_001_ch001",
      "order_index": 1,
      "title": "",
      "blocks": [
        {
          "block_id": "d2_literary_001_ch001_b0001",
          "order_index": 1,
          "page_ids": [],
          "block_type": "heading|paragraph|dialogue|footnote",
          "is_chapter_opening": false,
          "source_text": "",
          "clean_text": "",
          "sentences": [
            {"sent_id": "s001", "text": "", "span": [0, 0]}
          ],
          "discourse": {
            "speaker_entity_id": null,
            "addressee_entity_id": null,
            "pronoun_hints": []
          },
          "annotations": {
            "term_occurrences": [],
            "entity_mentions": [],
            "motifs": [],
            "tone": null,
            "implicit_meaning": null,
            "narrative_note": null
          },
          "reference_translation_id": null,
          "quality_flags": ["ok"],
          "provenance": {
            "raw_span": null,
            "corrected_by": null,
            "correction_note": null
          }
        }
      ]
    }
  ]
}
```

Manual reference subset không lưu trực tiếp trong `document.json`. Source of truth là `references/manual_reference_subset.jsonl`; block chỉ giữ `reference_translation_id` hoặc `null` để tránh hai bản reference bị lệch nhau.

### 5.1b. `chapter_summaries.jsonl`

Chapter summary không lưu trực tiếp trong `document.json`. Source of truth là sidecar `chapter_summaries.jsonl`, mỗi dòng là một chương. File này optional ở mức validator, nhưng nên có `summary_source` cho nguồn MVP vì nó giúp dataset tái dùng được cho long-document translation về sau.

```json
{
  "doc_id": "d2_literary_001",
  "chapter_id": "d2_literary_001_ch001",
  "summary_source": "1-3 sentence source-language summary of this chapter.",
  "source": "human",
  "characters_present": ["e_001", "e_002"],
  "key_events": ["event 1", "event 2"],
  "setting": "place/time context or null",
  "emotional_tone": "chapter-level tone or null",
  "motifs": ["time", "identity"],
  "summary_target": null,
  "open_threads": ["unresolved question if any"],
  "translation_notes": null,
  "confidence": 1.0
}
```

Required tối thiểu: `doc_id`, `chapter_id`, `summary_source`, `source`. `source` chỉ nhận `human` hoặc `ai_assisted_verified`; không đưa AI draft chưa duyệt vào bản freeze. Các field còn lại là optional và không nằm trong phần chấm bắt buộc của AI-LAB.

Quy trình vận hành: viết `summary_source` ngay sau khi annotate/review xong chương đó, không dồn toàn bộ việc tóm tắt về cuối dự án.

### 5.2. D4 `glossary.jsonl`

```json
{
  "term_id": "g_001",
  "doc_id": "d3_technical_001",
  "source_term": "machine learning",
  "expected_target": "học máy",
  "allowed_variants": ["học máy"],
  "forbidden_variants": ["máy học"],
  "domain": "CS",
  "chapter_scope": "global",
  "status": "locked",
  "occurrences": [
    {"block_id": "d3_technical_001_ch001_b0012", "span": [10, 26]}
  ],
  "annotated_by": "annotator_01",
  "confidence": 1.0
}
```

### 5.3. D4 `entities.jsonl`

```json
{
  "entity_id": "e_001",
  "doc_id": "d2_literary_001",
  "canonical_source": "Alice",
  "canonical_target": "Alice",
  "entity_type": "person",
  "gender": "female",
  "aliases_source": ["the girl"],
  "aliases_target": ["cô bé", "Alice"],
  "pronoun_policy": "cô bé/cô ấy",
  "mentions": [
    {"block_id": "d2_literary_001_ch001_b0001", "surface": "Alice", "span": [0, 5]}
  ],
  "annotated_by": "annotator_01",
  "confidence": 1.0
}
```

### 5.3b. `entity_relations.jsonl` (1.5.0)

Optional sidecar cho quan hệ giữa hai entity, chủ yếu phục vụ **xưng hô tiếng Việt** (cách một cặp gọi nhau và tự xưng). Một dòng = một edge có hướng. Quy ước: `source_entity` LÀ `relation_type` của `target_entity` (vd `relation_type=parent` ⇒ source là cha/mẹ của target).

```json
{
  "relation_id": "rel_a_b_02",
  "doc_id": "novel_01",
  "source_entity_id": "e_a",
  "target_entity_id": "e_b",
  "relation_type": "betrayed_friend",
  "state_label": "after_betrayal",
  "valid_from_block_id": "novel_01_ch09_b003",
  "valid_to_block_id": null,
  "trigger_event_id": "novel_01_ch09_event_betrayal",
  "address_policy": {
    "source_to_target": {"self_term": "tao", "address_term": "mày"},
    "target_to_source": {"self_term": "tôi", "address_term": "cậu"}
  },
  "evidence": [{"block_id": "novel_01_ch09_b003", "surface": "You betrayed me"}],
  "confidence": 0.74,
  "notes": "After the betrayal A turns hostile; B still speaks softly."
}
```

- `address_policy`: 4 chuỗi, hai chiều `source_to_target`/`target_to_source` × `self_term`/`address_term`. Hai chiều độc lập → bắt được lệch một phía.
- **Diễn biến quan hệ** (thân→thù): optional `state_label` + `valid_from_block_id` + `valid_to_block_id` (+ `trigger_event_id`, nhãn tự do, chưa có FK vì AI-LAB chưa có file events). Vắng các field pha = áp cả tài liệu. So range theo **document order (`order_index`)**, không so chuỗi.
- Bùng cảm xúc nhất thời (1 câu) KHÔNG tạo record mới → dùng `block.discourse.pronoun_hints` + `annotations.tone/narrative_note`.
- `relation_type` free-text + recommended values (sibling/parent/child/spouse/friend/master/servant/mentor/stranger/rival/creator_creation/guardian…); không enum cứng.
- Validator: `relation_id` duy nhất; `source/target_entity_id` resolve về entity; `valid_from/to_block_id` + `evidence[].block_id` resolve về block; **cảnh báo** nếu phase chồng lấp cùng cặp.
- Sản xuất (thesis runtime): **Relation Agent** ở pre-pass, sau Entity Agent, chỉ xuất relation có evidence. Phía AI-LAB: annotator điền, AI nháp + human duyệt (xem §6.0b).

### 5.4. D5 `injected_errors.jsonl`

```json
{
  "error_id": "d5_0001",
  "doc_id": "d2_literary_001",
  "block_id": "d2_literary_001_ch001_b0007",
  "clean_translation": "",
  "base_translation_source": "human_fixed|neutral_model|manual_reference_subset",
  "base_translation_model": null,
  "base_translation_version": "d5_base_v1",
  "corrupted_translation": "",
  "error_type": "terminology|entity|omission|addition|mistranslation|style|formula",
  "error_subtype": "T1.2_inconsistent_term",
  "severity": "critical|major|minor",
  "span_in_target": [0, 0],
  "expected_detection": true,
  "gold_fix": "",
  "created_by": "researcher_01"
}
```

Bản `clean_translation` dùng để chèn lỗi phải được freeze độc lập trước benchmark: do người tạo hiệu chỉnh, hoặc do một model trung lập sinh rồi người tạo kiểm tra. Không lấy base translation từ S0/S1/S2/S3/S3d để tránh injected-error set vô tình thiên vị một system.

### 5.5. D6 `relevance_queries.jsonl`

```json
{
  "query_id": "d6_0001",
  "doc_id": "d2_literary_001",
  "query_block_id": "d2_literary_001_ch002_b0010",
  "query_intent": "motif recall: falling / disorientation",
  "query_text": "",
  "relevant_memories": [
    {"memory_id": "summary_ch001", "memory_type": "summary", "relevance": 2},
    {"memory_id": "note_falling_001", "memory_type": "narrative_note", "relevance": 2},
    {"memory_id": "translation_b0003", "memory_type": "translation_memory", "relevance": 1}
  ],
  "pool_size": 50,
  "annotated_by": "annotator_01"
}
```

Relevance scale:

- `0`: không liên quan;
- `1`: liên quan một phần;
- `2`: rất liên quan, nên xuất hiện trong top-k.

---

## 6. Annotation Protocol

### 6.0. Vai trò: Schema / Pipeline / Agent / Annotator / Reviewer / Validator

Dataset tách rõ trách nhiệm để tránh nhầm "schema" với "agent hiểu nội dung":

| Thành phần | Trách nhiệm |
|---|---|
| **Schema** | Định nghĩa file nào tồn tại, field nào hợp lệ, field nào bắt buộc/optional, id liên kết ra sao, span tính theo đâu. Schema không tự hiểu nội dung. |
| **Pipeline** | Trích xuất nguồn, chia chapter/block, clean text, tạo `document.json`, quality flags và provenance. |
| **Agent/AI assistant** | Có thể đề xuất term/entity, chapter summary, tone, motif hoặc narrative note, nhưng output chỉ là draft cho người kiểm. |
| **Annotator** | Điền/sửa annotation theo guideline: term/entity, discourse, narrative hint, chapter summary, reference subset. |
| **Reviewer** | Xác nhận nội dung annotation. Dữ liệu freeze chỉ nhận mục đã được human review hoặc `ai_assisted_verified`. |
| **Validator** | Kiểm cấu trúc, enum, id, span và cross-file references. Validator không đánh giá summary/tone/implicit meaning có đúng văn học hay không. |

Quy ước vận hành: AI có thể hỗ trợ điền dữ liệu, nhưng không ghi thẳng vào bản freeze như dữ liệu thật nếu chưa có người duyệt. Các field như `source: "ai_assisted_verified"`, `status` đã duyệt theo từng file (`reviewed|locked`, `locked|human_verified`), `reviewed_by` và `confidence` dùng để truy vết mức độ tin cậy.

### 6.0b. Field ownership: code / LLM draft / human review

Schema 1.5.0 không giả định rằng một agent sẽ tự điền toàn bộ dataset. Mỗi nhóm field có executor phù hợp:

| Nhóm field | Executor chính | Quy tắc |
|---|---|---|
| Structure/id/order/source/provenance | Code/pipeline | Sinh deterministic; không sửa tay trong freeze |
| `sentences[]`, `span`, term/entity occurrence offsets | Code hoặc UI selection | Không giao LLM đếm offset ký tự |
| `clean_text`, `block_type`, `quality_flags` | Code draft + annotator review | Annotator sửa khi extraction/classification sai |
| Glossary/entity candidates, aliases, chapter summaries | LLM draft + annotator/reviewer verify | AI chỉ nháp, human chốt target/status |
| `motifs`, `tone`, `implicit_meaning`, `narrative_note` | Optional narrative hint | Chỉ điền khi có evidence; không bắt buộc mọi block |
| `reference_vi`, license, contamination risk, freeze decision | Human/reviewer/lead | Human-owned, review chéo, sign-off trước freeze |

Kết luận vận hành: 4 LLM agents của luận án không chịu trách nhiệm hoàn thiện schema offline. Dataset được tạo bằng pipeline + AI hỗ trợ + human review. Theo Directional Lock (RESEARCH_PLAN_V3 §0): pipeline thesis **tự dựng memory từ 0**, còn dataset AI-LAB đã QC là **gold để đánh giá (eval-only)**, không seed vào runtime.

### 6.0c. Dataset construction vs runtime agents vs evaluation

Cần tách rõ ba tầng để tránh hiểu nhầm "có 4 agents" nghĩa là agents sẽ tự hoàn thiện dataset:

| Tầng | Tạo/ghi bởi | Kiểm soát bởi | Vai trò trong nghiên cứu |
|---|---|---|---|
| **Dataset JSON offline** (`document.json`, `glossary.jsonl`, `entities.jsonl`, `chapter_summaries.jsonl`, `manual_reference_subset.jsonl`) | Extraction pipeline, annotator, reviewer, lead; AI chỉ được dùng làm draft | JSON Schema, `validate.py`, QC checklist, 4-eyes review, IAA, provenance/license sign-off | Làm source sạch và ground truth cho evaluation (gold, eval-only) |
| **Runtime memory T1-T7** | Seed từ dataset JSON; trong lúc chạy, Summary/Translator/Critic/Feedback có thể ghi thêm record có cấu trúc | Memory schema, retrieval logs, status/lock rules, Critic/human review khi cần | Cung cấp context cho Translator Agent trong hệ S2/S3 |
| **Retrieval/context pack** | Query Planner/Hybrid Retriever/Coverage Checker (infrastructure, không phải LLM agent) | D6 retrieval relevance, coverage logs, hard/soft separation | Chứng minh hệ lấy đúng context và không lấy thừa/lấy sai |
| **Interpretation Brief** | Narrative Understanding Agent | Log input/output, human spot-check, S3 vs S3d ablation | Nén ngữ cảnh narrative thành brief ngắn cho Translator |
| **Translation output** | Translator Agent | CriticAgent, human eval, MQM/MHP/BLP, TAR/ECS | Output cuối để đo chất lượng dịch |
| **Critic issues / QA memory** | CriticAgent Tier 1/2 | D5 injected errors, precision/recall, human audit | Đo khả năng phát hiện lỗi và quyết định accept/repair/human_review |

Hệ quả thiết kế:

1. **4 LLM agents không phải data annotators của AI-LAB.** Bốn agents của luận án (Summary, Narrative Understanding, Translator, Critic) thuộc runtime dịch. Chúng không được thiết kế để tự hoàn thiện dataset schema 1.5.0.
2. **Dataset là gold/đánh giá (eval-only), không phải input seed cho pipeline, cũng không phải output của Translator Agent.** Dataset được tạo bằng pipeline + human review; pipeline thesis tự dựng memory từ 0 (RESEARCH_PLAN_V3 §0).
3. **Schema pass không có nghĩa là nội dung đúng.** Validator chỉ bắt lỗi cấu trúc, enum, id, span, cross-file reference. Chất lượng term/entity/summary/tone/ẩn ý/reference VI phải được reviewer kiểm.
4. **Chia 4 agents là giảm độ phức tạp của runtime, không phải bằng chứng chất lượng.** Mỗi agent có nhiệm vụ hẹp hơn và có thể đo riêng, nhưng chất lượng S3 vẫn là giả thuyết phải kiểm chứng.
5. **Nếu S3 không tốt hơn baseline, đó là kết quả nghiên cứu hợp lệ.** So sánh S0/S1/S2/S3/S3d, D6, Critic P/R, và human evaluation mới là bằng chứng.

Mapping artifact -> measurement:

| Artifact / record | Dùng để làm gì | Đo/verify bằng |
|---|---|---|
| `glossary.jsonl` | Seed T1 terminology hard memory | TAR, glossary violation count, human spot-check |
| `entities.jsonl` | Seed T2 entity/pronoun memory | ECS, pronoun/entity consistency review |
| `chapter_summaries.jsonl` | Seed T4 chapter summary memory | S3 vs S3a, chapter-opening evaluation |
| `annotations.motifs/tone/implicit_meaning/narrative_note` | Seed T3/T4 narrative hints when available | S3 vs S3d, MHP/BLP/Likert narrative, human spot-check |
| `manual_reference_subset.jsonl` | Small reference/sanity subset, not full gold corpus | chrF/COMET only as secondary sanity; human review remains primary |
| retrieval log / selected memories | Diagnose context selection | D6 Recall@K, MRR, NDCG, precision/noise analysis |
| Critic issue log | Diagnose quality gate/repair | D5 precision/recall/F1 + human audit |

Quy tắc ngắn gọn: **Dataset construction được chứng minh bằng validation + QC; runtime agents được chứng minh bằng experiments; không dùng cái này thay cho cái kia.**

### 6.0d. Các điểm agent dễ fail và lớp chặn

Các rủi ro dưới đây là điểm cần chặn rõ trong tool/pipeline. Nếu fail âm thầm, dữ liệu hoặc luồng dịch có thể sai từ gốc.

| Rủi ro thực tế | Vì sao dễ fail | Lớp chặn bắt buộc |
|---|---|---|
| **Span/offset** (`occurrences`, `mentions`, `sentences[].span`) | LLM rất kém đếm offset ký tự và dễ bịa số | Giao cho code string-match hoặc UI selection; validator kiểm `end > start`; không cho LLM tự tính span |
| **Referential integrity** (`term_id`, `entity_id`, `reference_id`, `chapter_id`, `block_id`) | Agent có thể tạo ref treo/dangling hoặc id trùng | `validate.py` kiểm cross-file refs và duplicate id; nếu agent sinh JSON thì dùng validate -> repair tối đa 1 vòng |
| **Nhất quán xuyên cả cuốn** (`canonical_target`, `allowed_variants`, `forbidden_variants`, `pronoun_policy`) | Đây là quyết định dịch thuật dài hạn, LLM dễ đổi ý theo ngữ cảnh cục bộ | Với dataset offline: human verify/lock; trong runtime: hard memory đè soft context; conflict thì flag human/Critic, không tự chốt |
| **JSON schema strict** (`additionalProperties:false`, enum, required fields) | LLM dễ thêm field lạ, sai enum hoặc bỏ field required | Schema-constrained output khi có thể; luôn chạy `validate.py`; validate -> repair tối đa 1 vòng, không sửa tay tùy tiện |
| **Chi phí/scale** khi chạy LLM theo block/cả novel | Per-block LLM trên cả tác phẩm dài rất tốn token và dễ trễ tiến độ | Chunk theo chapter/block; MVP nhỏ; narrative fields optional; benchmark dùng subset rõ ràng trước khi mở rộng |
| **Narrative metadata bị bịa cho đủ ô** (`motifs`, `tone`, `implicit_meaning`, `narrative_note`) | Các field này chủ quan, LLM/annotator dễ suy diễn quá mức | Chỉ điền khi có evidence; không chắc thì để `[]`/`null`; reviewer spot-check; không dùng làm gold nếu chưa adjudicate |
| **Reference VI bị contamination bởi AI** | Nếu AI output bị coi là gold reference thì đánh giá luận án có thể bị vòng lặp | `manual_reference_subset.jsonl` chỉ nhận `reviewed/locked`; ghi `source=human|ai_assisted_verified`; raw AI draft chỉ nằm ở working log |

### 6.1. Term và Entity

Term annotation:

- chỉ annotate term có khả năng lặp lại hoặc cần nhất quán;
- ưu tiên term xuất hiện từ 2 lần trở lên, trừ named concept/term cực kỳ quan trọng;
- ưu tiên technical terms, named concepts, recurring literary objects;
- không annotate adverb/adjective đơn lẻ nếu không có vai trò dịch thuật;
- mỗi term có expected target, allowed variants, forbidden variants;
- mỗi occurrence có span/block_id.

Entity annotation:

- literary: annotate nhân vật, địa danh, tổ chức, vật thể có vai trò narrative;
- technical: annotate concept/module/function quan trọng;
- entity có canonical source/target, alias, pronoun policy;
- mention phải map về entity_id.

Disambiguation:

- dùng `domain`, `chapter_scope`, `status`;
- nếu cùng source term có nhiều target hợp lệ, annotation phải ghi rõ scope;
- conflict không tự resolve bằng fuzzy matching.

### 6.2. Discourse và Narrative

MVP chỉ cần:

- speaker/addressee nếu dialogue rõ;
- pronoun hints cơ bản;
- motif seed;
- tone label;
- narrative note ngắn khi có evidence.

Không yêu cầu full coreference resolver.

Narrative annotation dùng seed list:

```text
motif_id | definition | positive_examples | negative_examples
```

Annotator chỉ gán motif từ seed list. Motif mới chỉ là suggestion, không đưa vào test gold nếu chưa adjudicate.

Tone label nên là closed set, ví dụ:

- whimsical;
- ironic;
- tense;
- melancholic;
- formal_expository;
- instructional;
- neutral.

`implicit_meaning` chỉ ghi khi có evidence trong text. Nếu không rõ, để `null`.

### 6.3. MQM và Error Annotation

MQM rút gọn:

- Accuracy: omission, addition, mistranslation;
- Terminology;
- Entity;
- Fluency;
- Style/Narrative;
- Special content: formula/code/placeholder.

Mỗi issue cần:

- error_type;
- error_subtype theo taxonomy V3 nếu có;
- severity;
- source span;
- target span;
- evidence;
- gold fix nếu là D5.

### 6.4. MHP Human Preference

MHP là evaluation artifact, không phải dataset thô.

Protocol:

- reviewer chỉ đọc 2 bản dịch tiếng Việt;
- không xem source;
- không biết system label;
- A/B được random và swap;
- chọn A/B/Tie;
- chấm Likert narrative quality 1-5;
- thêm reason ngắn.

Tối thiểu 3 reviewers. Nếu đủ thời gian, 5-10 reviewers.

Anchor:

| Điểm | Mô tả |
|---|---|
| 1 | Đọc như dịch máy: đúng vài ý chính nhưng khô, cứng, mất giọng kể. Ví dụ câu văn Việt nghe như bám từng từ tiếng Anh. |
| 3 | Đọc ổn và khá đúng nghĩa, nhưng nhịp kể/từ ngữ chưa nhất quán; có đoạn tự nhiên, có đoạn còn máy móc. |
| 5 | Tự nhiên như văn kể tiếng Việt: giữ tone, nhịp, dụng ý và cảm xúc của đoạn; không thêm/bớt ý. |

### 6.5. Agreement

| Annotation | Metric | Target thực tế |
|---|---|---|
| Term/entity label | Cohen's kappa hoặc Fleiss' kappa | >= 0.70 |
| Motif occurrence | Cohen's/Fleiss' kappa | >= 0.50-0.60 |
| MQM severity | Cohen's/Fleiss' kappa | >= 0.60 |
| MHP preference | Fleiss' kappa + majority vote | kappa có thể thấp, cần báo cáo |

Nếu agreement thấp:

- ghi nhận limitation;
- adjudicate một subset;
- dùng majority vote;
- phân tích định tính các case bất đồng.

---

## 7. Cleaning và Normalization Rules

| Hạng mục | Quy định |
|---|---|
| Unicode | Normalize NFC |
| Whitespace | Collapse nhiều space thành 1 space trong prose; trim đầu/cuối |
| Line break | Bỏ line break do layout trong cùng paragraph; giữ block boundary |
| Hyphenation | Nối `under-\nstand` thành `understand`; giữ `well-known` |
| Quotes | Chuẩn hóa quote nhưng giữ dialogue marker |
| Header/footer/page number | Xóa khỏi body; có thể lưu vào provenance nếu cần |
| Footnote | Tách block `footnote`, không trộn vào paragraph |
| OCR noise | Cờ `ocr_suspect`; sửa thủ công nếu block thuộc test |
| Formula/code | Giữ nguyên hoặc thay bằng placeholder ổn định `[[FORMULA_001]]`; lưu mapping |
| Extraction error | Gắn `quality_flags=["extraction_error"]`; loại khỏi test nếu không sửa được |

Nguyên tắc vàng:

- `source_text` giữ raw extraction tương đối gần nguồn;
- `clean_text` là bản chuẩn hóa dùng để dịch/test;
- mọi sửa thủ công phải có correction note;
- không sửa raw source file.

---

## 8. Reference Translation và Ground Truth

### 8.1. Khi nào cần reference?

| Dataset | Reference translation |
|---|---|
| D1 | Cần, vì dùng chrF/COMET/BLEU |
| D2 | Không bắt buộc toàn bộ; cần một tập tối thiểu đã review để phân tích phụ, sau đó dịch/verify được càng nhiều càng tốt |
| D3 | Có thể có reference subset đã review; TAR/ECS/formula preservation là chính |
| D5 | Gold là lỗi injected và gold fix |
| D6 | Gold là relevance label |

Với literary translation, không dùng BLEU/chrF làm kết luận chính. Một bản dịch có thể rất tốt nhưng khác reference.

### 8.2. Reference VI capacity-based

Nguyên tắc:

- MVP cần một tập tối thiểu đã review để chứng minh quy trình, ưu tiên stratified: chapter opening, dialogue, motif-heavy, term-heavy, random;
- sau mức tối thiểu, dịch/verify được tới đâu đưa vào `manual_reference_subset` tới đó; càng nhiều càng tốt;
- không bắt buộc parallel VI toàn bộ, nhưng nếu nhóm có khả năng dịch gần/toàn bộ nguồn thì vẫn hợp lệ khi mọi dòng đều có review/provenance;
- dịch bởi một người song ngữ hoặc AI-assisted draft đã được người song ngữ sửa, review bởi một người khác;
- dùng cho qualitative analysis và sanity metrics; với literary translation, không dùng BLEU/chrF làm kết luận chính.

### 8.3. Bản dịch đã xuất bản

Không dùng bản dịch xuất bản làm reference chính vì:

- dễ bias metric overlap;
- có thể không phù hợp style mục tiêu;
- LLM có thể đã thấy trong training data;
- có vấn đề license/copyright.

Có thể dùng để đối chiếu định tính nếu license cho phép hoặc chỉ trích dẫn cực ngắn trong phạm vi hợp lý.

### 8.4. Contamination

Nếu dùng tác phẩm nổi tiếng như Alice:

- gắn `contamination_risk = high`;
- không dùng Alice làm nguồn duy nhất cho D2;
- thêm D2b: một truyện/đoạn ít phổ biến hơn hoặc không có bản dịch Việt công khai. Candidate có thể là truyện public-domain ngắn ít được dùng trong benchmark, ví dụ `The Yellow Wallpaper`, nhưng phải verify license/source và mức phổ biến khi thu thập;
- chạy contamination probe nhỏ: so sánh output của nhiều model trên một số đoạn nổi tiếng. Nếu nhiều model cho bản dịch quá giống nhau hoặc giống bản dịch xuất bản, đánh dấu risk cao hơn;
- báo cáo limitation.

---

## 9. Evaluation Mapping

| Dataset/Layer | Metric | System comparison | Mục đích |
|---|---|---|---|
| D1 | chrF, COMET/BERTScore, GEMBA-DA, BLEU phụ | S0/S1/S2/S3 | Sanity check sentence-level |
| D2 | ECS, TAR nếu có D4 term literary, MQM, MHP, BLP, GEMBA-DA phụ, Likert narrative, MATTR/MTLD | S0/S1/S2/S3/S3d | Narrative + entity + style |
| D2/D3 chapter-opening blocks | TAR/ECS/MQM | S3 vs S3a | Chapter summary impact |
| D3 | TAR, ECS, formula preservation, MQM | S0/S1/S2/S3 | Technical consistency |
| D4 | TAR/ECS gold | Used by E1 | Ground truth consistency |
| D5 | Precision, Recall, F1 | Critic Tier1/Tier2/Full | Critic detection effectiveness |
| D2/D3 critic-eligible blocks | TAR/ECS/MQM/preference, retry count | S3 vs S3b | CriticAgent impact on final translation quality |
| D6 | Recall@K, MRR, NDCG@K | exact vs +FTS/BM25 vs +vector | Retrieval quality |
| E-FEEDBACK | downstream TAR/ECS/preference | S3 vs S3c | Feedback impact |
| Process logs | MHR, token/block, cost, latency, retry count | all systems | Practical feasibility |

D6 nên dùng NDCG vì relevance là graded 0/1/2.

MATTR/MTLD phải dùng cùng một tokenizer VI cho mọi system output, ví dụ underthesea hoặc pyvi. GEMBA-DA chỉ là LLM-judge phụ, không thay thế MHP/BLP/MQM trong kết luận literary.

MHP/BLP:

- MHP: reviewer chỉ đọc tiếng Việt;
- BLP: judge đọc source + hai bản dịch;
- chạy A/B swap để giảm position bias;
- conflict sau swap tính là tie.

### 9.1. Công thức TAR và ECS

Terminology Accuracy Rate:

```text
TAR = số term occurrences được dịch đúng theo D4
      / tổng số term occurrences được đánh dấu trong D4
```

Một occurrence được tính đúng nếu target chứa `expected_target` hoặc một `allowed_variant`, và không chứa `forbidden_variant`.

Entity Consistency Score:

```text
ECS = số entity mentions dùng đúng canonical target/alias hợp lệ
      / tổng số entity mentions được đánh dấu trong D4
```

Với literary data, ECS không ép mọi mention phải lặp tên riêng. Pronoun/form of address hợp lệ được tính đúng nếu phù hợp `pronoun_policy` hoặc annotation context.

> Lưu ý đồng bộ: định nghĩa `entity_consistency` của CriticAgent Tier 1 phải khớp ECS ở đây. Pronoun, alias hoặc form of address hợp lệ theo `pronoun_policy` được tính **consistent**, không phải lỗi chỉ vì target không chứa `canonical_target`.

---

## 10. MVP và Full Scope

| Thành phần | MVP đủ bảo vệ | Full nếu dư thời gian |
|---|---|---|
| D1 | 300 sentence pairs | 1,000-2,000 sentence pairs |
| D2 | 1 main literary doc, 3 chapters, 80-120 blocks; nếu doc nổi tiếng/high contamination thì thêm D2b 20-30 blocks ít phổ biến | 2 docs, 5-8 chapters |
| D3 | 1 technical doc/chapter, khoảng 40-60 blocks | 3-5 chapters, 10k-20k words |
| D4 | 50 terms + 20 entities | 100 terms + 30 entities |
| D5 | 50 injected errors | 100 injected errors |
| D6 | 50 retrieval queries | 100 retrieval queries |
| MHP pairs | 50 pairs, >=3 reviewers | 50-100 pairs, 5-10 reviewers |
| Manual reference VI | Tối thiểu một tập stratified đã review; số lượng theo năng lực | Mở rộng càng nhiều càng tốt, có thể gần/toàn bộ nguồn nếu đủ review |

Nếu thiếu thời gian, thứ tự cắt:

1. Giảm D1 size.
2. Giảm D3 size.
3. Giảm số MHP pairs nhưng giữ >=30.
4. Không cắt D6 hoàn toàn; chỉ giảm còn 30 queries.
5. Không cắt D2b nếu D2 chính là tác phẩm nổi tiếng/high contamination; chỉ giảm kích thước D2b.
6. Không cắt D5 nếu vẫn cần đánh giá CriticAgent.

Minimum hard floor:

- D2 >= 50 blocks;
- D4 >= 30 terms/entities tổng hợp;
- D5 >= 30 injected errors;
- D6 >= 30 retrieval queries;
- MHP >= 30 pairs, >=3 reviewers.

### 10.1. Ước tính công annotate MVP

| Task | Nhân lực | MVP estimate |
|---|---|---|
| Clean/structure D2 literary 3 chapters | 1 người | 1-2 ngày |
| Clean/structure D3 technical subset | 1 người | 0.5-1 ngày |
| D4 term/entity annotation | 1 annotator + 1 checker | 8-12 giờ |
| D2 motif/tone/discourse annotation | 1 annotator + spot check | 6-10 giờ |
| D5 injected errors + gold fix | 1 người | 4-6 giờ |
| D6 retrieval queries + graded relevance | 1 người | 3-5 giờ |
| MHP review | >=3 reviewers | 1-2 giờ/reviewer |

Tổng MVP thực tế khoảng 2-3 tuần nếu làm cẩn thận, hoặc nhanh hơn nếu chỉ pilot 1 document và giảm D3/D1.

---

## 11. Enterprise-Style Dataset Checklist

```text
[ ] Completeness
    - Mọi block có doc_id/chapter_id/block_id/order_index/clean_text.
    - Mỗi RQ có dataset subset tương ứng.

[ ] Cleanliness
    - Không có block rỗng trong test set.
    - OCR/extraction errors đã được flag hoặc sửa.

[ ] Consistency
    - ID unique.
    - Annotation refs trỏ tới block/entity/term tồn tại.
    - JSON schema validate pass.

[ ] Traceability
    - raw -> clean -> annotation có provenance.
    - Có source URL/path, sha256, extraction tool, pipeline version.

[ ] Reproducibility
    - Có versions.json và CHANGELOG.
    - Test set frozen trước khi chạy benchmark chính.
    - Có pilot/dev subset tách khỏi test để tune top_k, thresholds, prompt và extraction rules.

[ ] Licensing
    - Mỗi source có license rõ.
    - License được kiểm tra lại tại thời điểm thu thập.
    - Không dùng nguồn không rõ quyền.

[ ] No leakage
    - Gold annotations độc lập với system output.
    - Reviewer không thấy system label.
    - Injected errors không sinh từ system under test.
    - Tuning/dev subset không được dùng trong báo cáo test chính.
    - MHP pair record nội bộ có thể lưu system id, nhưng form đưa cho reviewer chỉ có A/B/Tie, không có nhãn hệ thống.

[ ] Auditability
    - Có annotator id.
    - Có agreement report.
    - Có adjudication log cho case bất đồng.
    - Pilot test 10-20 blocks đã chạy trước khi annotate full.
    - Các lỗi extraction phổ biến như hyphenation, header/footer, empty block, mojibake được grep/check tự động.
```

---

## 12. Risks và Cách Xử Lý

| Rủi ro | Hệ quả | Giảm thiểu |
|---|---|---|
| Dataset quá nhỏ | Không đủ statistical power | Paired comparison trên cùng block, bootstrap CI, effect size |
| Dataset quá sạch | Không lộ lợi ích memory/critic | Chọn doc có term lặp, dialogue, motif; dùng D5 injected errors |
| Alice/Snow White contamination | S3/S3d có thể bị bias vì model đã thấy dữ liệu | Gắn risk, thêm D2b ít phổ biến, không dùng published translation làm reference chính |
| Narrative annotation chủ quan | Agreement thấp | Seed motif list, closed tone labels, evidence bắt buộc, majority/adjudication |
| Human preference agreement thấp | Kết luận yếu | Tăng reviewer, dùng tie, phân tích định tính, báo cáo kappa |
| S3 không hơn S3d | Có thể do retrieval, brief, model hoặc metric | Dùng D6 để chẩn đoán retrieval; nếu D6 tốt mà S3 không hơn, negative result vẫn hợp lệ |
| License không rõ | Không thể công bố dataset | Chỉ dùng source public domain/CC rõ, lưu license note |
| JSON schema đổi liên tục | Benchmark không tái lập | Freeze schema version, mọi đổi sau freeze tăng version |

Negative results không làm đề tài thất bại nếu dataset đủ chẩn đoán. Ví dụ, nếu D6 cho thấy retrieval tốt nhưng S3 không hơn S3d, luận văn có thể kết luận Narrative Brief chưa tạo tác động rõ với model/prompt hiện tại.

---

## 13. Quyết Định Hiện Tại

| Quyết định | Trạng thái |
|---|---|
| Dataset source of truth là JSON sạch | Chốt |
| Giữ D1-D6, nhưng D6 là bắt buộc nhỏ | Chốt |
| MHP là evaluation artifact, không phải dataset thô riêng | Chốt |
| Feedback là experiment protocol, không phải dataset riêng | Chốt |
| D2 không lệ thuộc reference translation | Chốt |
| D1/D3 có thể giảm scope nếu quá tải | Chốt |
| Published Vietnamese translations không dùng làm reference chính | Chốt |
| Cần D2b ít phổ biến nếu dùng Alice/Snow White | Chốt |
| D5 base translation phải freeze độc lập, không lấy từ S0-S3d | Chốt |
| Tuning/dev subset tách khỏi official test set | Chốt |
| Manual reference subset lưu ở file riêng, block chỉ trỏ `reference_translation_id` | Chốt |
| Chapter summary lưu ở `chapter_summaries.jsonl`, không dùng `summary_seed` trong `document.json` | Chốt |

## 14. Bước Tiếp Theo

1. Chốt với thầy về D1-D6 và mức MVP.
2. Chọn nguồn candidate cho D2/D3, chỉ kiểm tra license/provenance, chưa annotate.
3. Viết JSON Schema chính thức trong `dataset/schema/`.
4. Tạo một pilot document 10-20 blocks để kiểm tra extraction -> clean_text -> annotation -> evaluation.
5. Sau pilot mới freeze schema và bắt đầu annotate MVP.
