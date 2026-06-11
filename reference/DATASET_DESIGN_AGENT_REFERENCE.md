# THIẾT KẾ DATASET CHO NGHIÊN CỨU DỊCH MÁY ANH-VIỆT VĂN BẢN DÀI DỰA TRÊN AGENT

**File này:** Thiết kế chi tiết bộ dataset phục vụ đánh giá hệ thống agent-based long-document EN-VI translation trong `RESEARCH_PLAN_V3.md`.

**Nguồn tham chiếu:** `RESEARCH_PLAN_V3.md` (Section 10: Dataset Suite), `PROMPT_DESIGN.md`, `RELATED_WORK.md`.

**Nguyên tắc cốt lõi:** Dataset không phải một khối duy nhất. Mỗi thành phần phục vụ một câu hỏi nghiên cứu cụ thể. Dataset được thiết kế như một **evaluation suite** — có thể bổ sung theo thời gian, nhưng MVP phải đủ chuẩn để bảo vệ.

---

## 1. Dataset phục vụ những câu hỏi nghiên cứu nào?

Bảng dưới đây cho thấy từng câu hỏi nghiên cứu (RQ) được đo bằng dataset nào, với metric nào.

| RQ | Câu hỏi | Dataset subsets | Metric chính | Metric phụ |
|----|----------|----------------|---------------|------------|
| RQ1 | Memory system có cải thiện consistency không? | D2, D3, D4 | TAR, ECS | chrF (nếu có ref) |
| RQ2 | Chapter summary memory có cải thiện đầu chapter mới không? | D2, D3 | TAR, ECS, MQM | chrF |
| RQ3 | CriticAgent phát hiện lỗi được không? | D5 | Precision, Recall, F1 | — |
| RQ4 | Feedback có cải thiện downstream blocks không? | D2, D3 | TAR, ECS, MQM | Human preference |
| RQ5 | Narrative Understanding Agent có cải thiện giọng kể không? | D2 | MHP, Likert narrative quality, BLP | MATTR, MTLD |
| — | Hybrid retrieval có lấy đúng narrative context không? | D6 | Recall@K, MRR, NDCG | Human relevance score |
| — | Hệ thống có làm giảm chất lượng dịch cơ bản không? | D1 | chrF, COMET, GEMBA-DA | BLEU |

**Thiết kế chú ý:** RQ5 (narrative quality) là câu hỏi khó đo nhất vì phụ thuộc human agreement. D2 phải đủ giàu motif, dialogue và emotional tone để tạo ra sự khác biệt đo được giữa S3 và S3d.

---

## 2. Phân nhóm dataset: bao nhiêu nhóm là đủ?

### 2.1. Phản biện: có nên tách hay gộp?

**Gộp tất cả vào một dataset** có vẻ đơn giản nhưng có ba vấn đề:

1. **Motif và narrative** chỉ xuất hiện trong văn chương, không có trong tài liệu kỹ thuật. Nếu gộp, D2 (literary) bị pha loãng bởi D3 (technical).
2. **Ground truth cho consistency** (D4) cần được annotate thủ công với term/entity cụ thể — không tự động sinh từ raw text.
3. **Injected-error set (D5)** phải tạo có kiểm soát, không phải từ dữ liệu thật. Nếu gộp, không thể đo precision/recall của CriticAgent một cách sạch.

**Kết luận: giữ 6 nhóm (D1-D6) như V3 đã đề xuất, nhưng với rationale rõ hơn.**

### 2.2. Mỗi nhóm bắt buộc hay tùy chọn?

| Nhóm | Bắt buộc? | Lý do |
|-------|-----------|-------|
| **D1** — Sentence-level reference | **Có** | Là cách duy nhất đo chrF/COMET/GEMBA-DA một cách có ground truth. Không thể thay thế bằng human eval. |
| **D2** — Literary document | **Có** | Dataset chính cho narrative quality (RQ5) — không có D2 thì không đo được giọng kể. |
| **D3** — Technical document | **Có** | Dùng để đo glossary/terminology (RQ1, RQ2) trong domain có nhiều term lặp lại — không thể dùng D2 cho mục đích này. |
| **D4** — Term/entity ground truth | **Có** | Không có D4 thì không đo được TAR/ECS một cách chuẩn. |
| **D5** — Injected-error | **Có** | Không có D5 thì không đo được precision/recall của CriticAgent (RQ3). |
| **D6** — Retrieval relevance | **Tùy chọn** | Hữu ích nhưng có thể ước lượng từ D2. Nếu thiếu thời gian, giảm scope. |

### 2.3. Tại sao không tách thêm?

Không cần tách thêm nhóm vì:

- **Không cần tách MHP evaluation set riêng** — MHP dùng chính bản dịch từ D2 của S3 vs S3d, không cần dataset riêng.
- **Không cần tách "cold-start" set** — D2 đã chứa chapter đầu tiên, đủ để đo cold-start của Narrative Agent.
- **Không cần "dev set" riêng** — ước lượng hyperparameter (threshold, top_k) có thể dùng chính D2 nhưng đo trên một phần nhỏ, không cần chia đủ-thì.

---

## 3. Pipeline chuyển PDF sang JSON sạch

### 3.1. Sơ đồ tổng quát

```
Input (PDF/EPUB/TXT)
    │
    ▼
┌─────────────────────┐
│  Format Detection    │ ← tự động nhận diện loại input
└────────┬────────────┘
         │
    ┌────┴────┬────────────┐
    ▼         ▼            ▼
 born-digital  scanned    EPUB/TXT
   PDF         PDF        (ưu tiên)
    │          │            │
    ▼          ▼            ▼
┌─────────┐ ┌─────────┐ ┌─────────┐
│ PyMuPDF │ │Tesseract │ │ ebooklib│
│pdfplumber│ │ + layout │ │  + HTML │
│  strip   │ │  parser  │ │  parse  │
└────┬────┘ └────┬────┘ └────┬────┘
     │           │            │
     ▼           ▼            ▼
┌──────────────────────────────────────┐
│  Raw Text Extraction + Layout Segreg  │
│  (headers/footers/page# separated)   │
└──────────────────┬───────────────────┘
                   │
                   ▼
┌──────────────────────────────────────┐
│  Structural Parsing                   │
│  Chapter detection / Section / Block  │
│  Dialogue detection                   │
│  Sentence splitting                   │
└──────────────────┬───────────────────┘
                   │
                   ▼
┌──────────────────────────────────────┐
│  Pre-cleaning                        │
│  Whitespace / hyphenation / OCR noise│
└──────────────────┬───────────────────┘
                   │
                   ▼
┌──────────────────────────────────────┐
│  Quality Check (automatic)            │
│  - Length sanity                     │
│  - Script uniformity                 │
│  - Empty block detection             │
└──────────────────┬───────────────────┘
                   │
                   ▼
┌──────────────────────────────────────┐
│  Manual Correction (targeted)        │
│  - Low-confidence extraction blocks  │
│  - Formula / code blocks             │
│  - Edge cases                        │
└──────────────────┬───────────────────┘
                   │
                   ▼
┌──────────────────────────────────────┐
│  JSON Export + Versioning            │
│  Structured document with metadata    │
└──────────────────────────────────────┘
```

### 3.2. Chi tiết từng bước

**Bước 1 — Format Detection**

| Loại input | Tool ưu tiên | Lý do |
|------------|-------------|-------|
| Born-digital PDF (text layer) | `pdfplumber` trước, fallback `PyMuPDF` | `pdfplumber` giữ được bounding box tốt cho layout segregation |
| Scanned PDF | Tesseract OCR + `pytesseract` | Cần OCR engine; output cần post-processing nhiều |
| EPUB | `ebooklib` + custom HTML parser | EPUB là text markup — parsing chính xác hơn PDF |
| Plain TXT | Custom parser với encoding detection | Cần detect UTF-8/UTF-16 BOM; tách chapter bằng heading pattern |

**Bước 2 — Layout Segregation**

Tách riêng các thành phần trước khi ghép block:

| Loại thành phần | Cách nhận diện | Xử lý |
|-----------------|-----------------|--------|
| Header / Footer | Vị trí cố định (top/bottom), font size nhỏ, text ngắn | Bỏ hoặc gắn tag `page_header`/`page_footer` |
| Page number | Regex `[0-9]+` trên dòng riêng, gần margin | Bỏ |
| Footnote | Bounding box nhỏ ở bottom, superscript reference | Bỏ hoặc gắn tag `footnote` |
| Chapter heading | Font size lớn, all-caps hoặc pattern "Chapter N" | Gắn tag `chapter_heading` |
| Section heading | Font size trung bình, bold | Gắn tag `section_heading` |
| Body text | Font size ổn định, không thuộc các loại trên | Ghép thành block |
| Dialogue | Cặp ngoặc kép `"..."` hoặc dash `-` đầu dòng | Gắn tag `dialogue` |
| Formula / Code | Inline: `$...$` hoặc `` `...` ``; Block: line riêng, không có prose | Gắn tag `formula`/`code` |
| List | Dòng bắt đầu bằng `-`, `*`, `1.`, `a)` | Gắn tag `list_item` |

**Bước 3 — Chapter/Section/Block Segmentation**

```
Chapter Detection:
├── Pattern: "Chapter N", "CHƯƠNG N", Roman numeral, 
│           blank line + heading pattern
├── Fallback: Tesseract page where font size changes
└── Validate: chapter title length ∈ [5, 100] chars

Block Segmentation:
├── Max block size: 300-600 tokens (~1200-2500 chars EN)
├── Split at paragraph boundary (blank line) trước
├── Nếu paragraph > max_size → split at sentence boundary
└── Đánh số: {doc_id}_ch{n}_b{n}

Dialogue Detection:
├── Pattern 1: Quoted speech ("..." hoặc '...')
├── Pattern 2: Dash-prefixed lines (— Speaker:)
├── Nếu dialogue: gắn speaker/addressee tags
└── Speaker: extract từ pattern "— Name:" hoặc quoted attribution
```

**Bước 4 — Sentence Splitting**

Dùng `spacy` với model `en_core_web_sm` cho tiếng Anh. Cấu hình:

```python
# Cấu hình spacy sentence splitter
nlp = spacy.load("en_core_web_sm")
# Disable các component không cần để tăng tốc
nlp.disable_pipes(["ner", "lemmatizer"])
# Custom boundary: không split trong quoted dialogue
# Custom boundary: giữ công thức inline nguyên
```

### 3.3. Versioning

Mỗi lần corpus được hiệu chỉnh, tạo version tag:

```
datasets/
├── raw/
│   ├── alice_wonderland/
│   │   ├── v1.0_raw/          # Raw extraction, chưa clean
│   │   └── v1.1_corrected/    # Đã manual correct
│   └── technical_set/
│       └── v1.0_raw/
└── processed/
    ├── alice_wonderland/
    │   ├── v1.0_clean/        # JSON sau cleaning
    │   └── v1.1_clean/
    └── technical_set/
        └── v1.0_clean/
```

Ghi log thay đổi trong `CHANGELOG.md` mỗi version.

---

## 4. Công cụ theo loại nguồn

### 4.1. Born-digital PDF

| Tool | Trường hợp dùng | Ưu điểm | Nhược điểm |
|------|-----------------|---------|-----------|
| `pdfplumber` | Trích text chính, giữ bounding box | API đơn giản, tốt cho text extraction | Chậm với PDF lớn |
| `PyMuPDF` (fitz) | Tách page, render image, text extraction nhanh | Nhanh, hỗ trợ search | Bounding box kém chính xác hơn pdfplumber |
| `pdfminer.six` | Text extraction chi tiết (fonts, positions) | Giữ được font metadata | API phức tạp |
| `unstructured` | Full pipeline: text + table + formula | Tất cả trong một | Đôi khi over-engineer |

**Khuyến nghị:** Dùng `pdfplumber` cho D3 (technical) vì cần giữ được table/formula layout. Dùng `PyMuPDF` cho D2 (literary) vì chủ yếu là prose text, cần tốc độ.

### 4.2. Scanned PDF

| Tool | Mô tả |
|------|-------|
| Tesseract OCR (`pytesseract`) | OCR engine chính; cần `tesseract` binary cài trên máy |
| `EasyOCR` | Python wrapper, hỗ trợ GPU, độ chính xác tốt hơn Tesseract cho văn bản thường |
| `PaddleOCR` | Cần thì dùng, tốt cho layout phức tạp |

**Quy trình OCR:**
1. Render page → image (PyMuPDF)
2. Pass qua OCR engine
3. Post-process: loại bỏ noise, correct common OCR errors
4. Áp lại structural parsing

**Lưu ý:** Scanned PDF cần human check sau OCR. Đánh dấu confidence score. Các block có confidence < 0.7 → manual review.

### 4.3. EPUB/TXT

| Tool | Mô tả |
|------|-------|
| `ebooklib` | Parse EPUB, lấy chapter list, HTML content |
| `beautifulsoup4` | Parse HTML output của ebooklib, extract text |
| Custom regex parser | Xử lý plain TXT |

**Ưu tiên EPUB/TXT khi có:** Vì EPUB là markup-based, parsing chính xác hơn PDF. Alice in Wonderland có bản EPUB chính thức trên Project Gutenberg.

### 4.4. Public domain literature

| Nguồn | URL | License | Ghi chú |
|--------|-----|---------|---------|
| Project Gutenberg | gutendex.com, gutenberg.org | Public Domain | Kho văn chương lớn nhất, nhiều bản plain text |
| Standard Ebooks | standardebooks.org | CC0 | Chất lượng cao, đã clean và proofread |
| Internet Archive | archive.org | Mixed | Cần kiểm tra license từng item |

**Khuyến nghị cho D2:**
- Ưu tiên Standard Ebooks vì đã có quality assurance.
- Backup: Project Gutenberg.
- Không dùng bản đã có bản dịch tiếng Việt công khai (Alice đã có bản dịch NXB Kim Đồng, Đặng Thế Bính — xem §12 về contamination).

### 4.5. Technical documents

| Nguồn | License | Ghi chú |
|--------|---------|---------|
| OpenStax | CC BY-NC-SA 4.0 | Sách giáo khoa miễn phí, có PDF + EPUB, nhiều công thức |
| Khan Academy | CC BY-NC-SA | Tài liệu học tập, đa dạng môn |
| LibreTexts | CC BY-NC-SA | Tài liệu đại học, nhiều STEM |

**Khuyến nghị:** OpenStax là ưu tiên hàng đầu vì:
- License rõ ràng (CC BY-NC-SA 4.0)
- Có cấu trúc chapter/section rõ ràng
- Nhiều công thức toán (phù hợp đo formula preservation)
- Định dạng chuẩn, ít scanned

---

## 5. Dataset JSON Schema

### 5.1. Cấu trúc thư mục

```
datasets/
├── D1_sentence_level/
│   └── flores200_sample/
│       ├── metadata.json
│       ├── sentences_en.jsonl
│       ├── sentences_vi_ref.jsonl
│       └── schema.json
├── D2_literary/
│   └── alice_wonderland/
│       ├── metadata.json
│       ├── document.json        # Toàn bộ document
│       ├── annotations/         # Annotation files
│       │   ├── terms.jsonl
│       │   ├── entities.jsonl
│       │   ├── motifs.jsonl
│       │   └── issues.jsonl
│       └── samples/             # Benchmark samples
│           └── evaluation_pairs.jsonl
├── D3_technical/
│   └── openstax_cs/
│       ├── metadata.json
│       ├── document.json
│       ├── annotations/
│       │   ├── terms.jsonl
│       │   └── formulas.jsonl
│       └── samples/
│           └── evaluation_pairs.jsonl
├── D4_ground_truth/
│   └── ground_truth.jsonl
├── D5_injected_errors/
│   └── injected_*.jsonl
├── D6_retrieval/
│   └── retrieval_queries.jsonl
└── common/
    ├── annotation_guidelines/
    │   ├── term_annotation_guide.md
    │   ├── entity_annotation_guide.md
    │   ├── motif_annotation_guide.md
    │   └── quality_annotation_guide.md
    └── evaluation/
        ├── mhp_form.md
        └── likert_anchors.md
```

### 5.2. Document-level schema (`document.json`)

```json
{
  "schema_version": "1.0",
  "doc_id": "alice_wonderland",
  "title": "Alice's Adventures in Wonderland",
  "author": "Lewis Carroll",
  "language_source": "en",
  "language_target": "vi",
  "license": "public_domain",
  "license_url": "https://www.gutenberg.org/ebooks/11",
  "source_format": "gutenberg_plain_text",
  "extraction_date": "2026-01-15",
  "extraction_version": "v1.0_raw",
  "cleaning_version": "v1.0_clean",
  "extraction_confidence_avg": 0.95,
  "total_words": 26470,
  "total_chapters": 12,
  "total_blocks": 142,
  "metadata": {
    "original_pub_year": 1865,
    "genre": "literary_fantasy",
    "has_dialogue": true,
    "has_narrative": true,
    "has_technical_terms": false,
    "has_formulas": false
  },
  "chapters": [
    {
      "chapter_id": "ch01",
      "chapter_title": "Down the Rabbit-Hole",
      "chapter_number": 1,
      "blocks": [
        {
          "block_id": "alice_ch01_b001",
          "chapter_id": "ch01",
          "block_index": 1,
          "block_type": "narrative",
          "source_text": "Alice was beginning to get very tired...",
          "source_text_clean": "Alice was beginning to get very tired of sitting by her sister on the bank...",
          "speaker": null,
          "addressee": null,
          "has_dialogue": false,
          "dialogue_spans": [],
          "word_count": 87,
          "has_entity_mention": true,
          "entity_mentions": ["Alice"],
          "has_formula": false,
          "extraction_confidence": 0.98,
          "is_quality_flagged": false,
          "flag_reason": null,
          "parent_doc": "alice_wonderland"
        },
        {
          "block_id": "alice_ch01_b002",
          "chapter_id": "ch01",
          "block_index": 2,
          "block_type": "dialogue",
          "source_text": "\"What a curious feeling!\" said Alice...",
          "source_text_clean": "\"What a curious feeling!\" said Alice...",
          "speaker": "Alice",
          "addressee": null,
          "has_dialogue": true,
          "dialogue_spans": [
            {"speaker": "Alice", "start": 0, "end": 24}
          ],
          "word_count": 42,
          "has_entity_mention": true,
          "entity_mentions": ["Alice", "the White Rabbit"],
          "has_formula": false,
          "extraction_confidence": 0.97,
          "is_quality_flagged": false,
          "flag_reason": null,
          "parent_doc": "alice_wonderland"
        }
      ]
    }
  ]
}
```

### 5.3. Ground truth schema (`ground_truth.jsonl` — D4)

```json
{
  "gt_id": "gt_alice_001",
  "doc_id": "alice_wonderland",
  "type": "term",
  "source_term": "the White Rabbit",
  "expected_target": "Thỏ Trắng",
  "allowed_variants": ["Con Thỏ Trắng"],
  "forbidden_variants": ["White Rabbit (giữ nguyên)"],
  "domain": "literary",
  "chapter_scope": "global",
  "occurrences": [
    {
      "block_id": "alice_ch01_b002",
      "source_span": "the White Rabbit",
      "position_in_block": "exact",
      "note": "Lần xuất hiện đầu tiên, cần dịch ngay"
    },
    {
      "block_id": "alice_ch01_b015",
      "source_span": "White Rabbit",
      "position_in_block": "partial",
      "note": "Bỏ 'the', cần nhất quán với canonical name"
    }
  ],
  "annotated_by": "researcher_01",
  "annotation_date": "2026-02-01",
  "confidence": 0.95
}
```

```json
{
  "gt_id": "gt_alice_e001",
  "doc_id": "alice_wonderland",
  "type": "entity",
  "canonical_source": "Alice",
  "canonical_target": "Alice",
  "entity_type": "person",
  "gender": "female",
  "role": "protagonist",
  "aliases_source": ["the girl", "she", "the child"],
  "aliases_target": ["cô bé", "cô ấy", "Alice"],
  "preferred_vietnamese_forms": {
    "narrative": "Alice",
    "pronoun_when_referenced": "cô ấy"
  },
  "valid_from_block": "alice_ch01_b001",
  "valid_to_block": "alice_ch12_b999",
  "occurrences": [
    {
      "block_id": "alice_ch01_b001",
      "mention_form": "Alice",
      "context": "Alice was beginning to get very tired..."
    }
  ],
  "annotated_by": "researcher_01",
  "annotation_date": "2026-02-01",
  "confidence": 0.98
}
```

### 5.4. Injected-error schema (`injected_*.jsonl` — D5)

```json
{
  "injected_id": "ie_alice_001",
  "doc_id": "alice_wonderland",
  "source_block_id": "alice_ch01_b010",
  "source_text": "The White Rabbit put on his spectacles.",
  "source_translation_good": "Thỏ Trắng đeo cặp kính vào.",
  "injected_translation": "Con thỏ trắng đeo kính.",
  "error_type": "terminology",
  "error_subtype": "T1.2",
  "error_description": "Dịch không nhất quán: 'the White Rabbit' đã chốt là 'Thỏ Trắng' ở chương 1, lỗi đổi thành 'con thỏ trắng' (không viết hoa, thêm 'con')",
  "severity": "major",
  "expected_fixes": [
    "Thỏ Trắng đeo cặp kính vào."
  ],
  "injector_note": "Tạo bằng cách thay canonical target bằng variant không chuẩn",
  "is_recoverable": true,
  "detectability": "Tier 1 (rule-based glossary adherence)"
}
```

### 5.5. Retrieval relevance schema (`retrieval_queries.jsonl` — D6)

```json
{
  "query_id": "rq_alice_001",
  "doc_id": "alice_wonderland",
  "query_block_id": "alice_ch06_b008",
  "query_description": "Block chứa lời nhắc lại motif 'falling down the rabbit hole' từ chương 1",
  "query_source_text": "She still remembered the falling feeling...",
  "expected_relevant_memories": [
    {
      "memory_type": "T4_summary",
      "memory_id": "alice_ch01_summary",
      "relevance_score": 2,
      "why_relevant": "Chapter 1 summary chứa motif 'falling' và bối cảnh rabbit hole"
    },
    {
      "memory_type": "T3_narrative_note",
      "memory_id": "alice_ch01_b003_note",
      "relevance_score": 2,
      "why_relevant": "Narrative note ghi nhận 'falling' là motif xuất hiện lần đầu"
    },
    {
      "memory_type": "T5_translation_memory",
      "memory_id": "alice_ch01_b003_tm",
      "relevance_score": 1,
      "why_relevant": "Translation memory từ block chương 1 chứa 'down the rabbit-hole'"
    }
  ],
  "relevance_scale": "0 = không liên quan, 1 = liên quan, 2 = rất liên quan"
}
```

### 5.6. MHP evaluation pairs schema

```json
{
  "pair_id": "mhp_alice_001",
  "doc_id": "alice_wonderland",
  "block_id": "alice_ch05_b007",
  "source_text": "The Hatter's Tea Party was in full swing...",
  "translation_system_a": "S3",
  "translation_a": "Bữa Tiệc Trà của Mũ Lúp xúp đang diễn ra rất nhộn nhịp...",
  "translation_system_b": "S3d",
  "translation_b": "Bữa tiệc trà của Người Đội Mũ Lúp đang diễn ra hết sức náo nhiệt...",
  "stratification_category": "dialogue_rich",
  "swap_position": false,
  "reviewer_id": null,
  "mhp_choice": null,
  "likert_narrative_a": null,
  "likert_narrative_b": null,
  "likert_adequacy_a": null,
  "likert_adequacy_b": null,
  "free_text_reason": null
}
```

---

## 6. Annotation Protocol

### 6.1. Term/Entity Annotation (D4)

**Người annotate:** Người nghiên cứu (hoặc 1 assistant có hướng dẫn).

**Quy trình:**

```
Bước 1: Đọc toàn bộ document (hoặc 3 chương đầu cho MVP)
Bước 2: Trích xuất candidate terms (bằng regex hoặc frequency)
  - Danh từ riêng / thuật ngữ xuất hiện ≥3 lần
  - Tên nhân vật / địa danh
Bước 3: Với mỗi candidate:
  - Xác định canonical source form
  - Quyết định canonical target (tra cứu, hoặc đề xuất)
  - Liệt kê allowed variants
  - Liệt kê forbidden variants (nếu biết)
  - Đánh dấu domain
  - Trace tất cả occurrences
Bước 4: Review cross-check
  - Người thứ hai check 20% entries ngẫu nhiên
  - Đo inter-annotator agreement (Cohen's κ)
```

**Term annotation guideline:**

```
QUY TẮC TERM ANNOTATION:
1. Chỉ annotate term xuất hiện ≥ 2 lần trong document.
2. Term chỉ là danh từ/cụm danh từ; không annotate verb/adjective đơn lẻ.
3. "Alice" → annotate (entity). "machine learning" → annotate (term).
   "quickly" → không annotate (adverb thường).
4. Với technical document (D3): annotate thuật ngữ chuyên ngành.
   Với literary document (D2): annotate entity + danh từ riêng + danh từ có ý nghĩa đặc biệt.
5. Canonical target:
   - Nếu đã có bản dịch phổ biến (Alice = "Alice") → dùng.
   - Nếu chưa có → đề xuất 1-2 phương án.
   - Nếu có nhiều phương án → đánh dấu là ambiguous.
6. Allowed variants: các dịch có thể chấp nhận (ví dụ: "Alice" có thể = "Alice" hoặc "cô bé Alice").
7. Forbidden variants: các dịch sai hoặc không nên dùng.
```

**Entity annotation guideline:**

```
QUY TẮC ENTITY ANNOTATION:
1. Với literary (D2): annotate mọi nhân vật, địa danh, tổ chức có tên riêng.
2. Với technical (D3): annotate tên hàm, tên module, tên khái niệm quan trọng.
3. Alias tracking: ghi cả dạng xuất hiện trong text (source) và dạng đích đã chốt.
4. Giới tính: xác định nếu biết, null nếu không rõ.
5. Valid_from/valid_to: block đầu tiên và cuối cùng entity xuất hiện.
   Null nếu entity xuất hiện xuyên suốt.
```

### 6.2. Motif/Tone/Narrative Annotation (D2)

**Nguyên tắc cốt lõi: Human-seeded, không fully automatic.**

**Motif seed list (seed trước, do người nghiên cứu định nghĩa):**

Với Alice in Wonderland, seed 5-7 motifs chính:

```
MOTIF SEED LIST — Alice in Wonderland:
1. size_change: Alice thay đổi kích thước (lớn lên / nhỏ lại)
2. falling: Alice rơi xuống (rabbit hole)
3. identity_questioning: Alice đặt câu hỏi về danh tính ("Who am I?")
4. wordplay: Carroll chơi chữ (dùng từ theo nghĩa đen / nghĩa bóng)
5. absurd_logic: logic ngược đời (hội hóa trà)
6. lost_or_finding: Alice lạc / tìm đường
```

**Motif annotation protocol:**

```
Bước 1: Người nghiên cứu đọc 3 chương đầu, seed motifs.
Bước 2: Với mỗi block:
  - Kiểm tra có chứa motif seed không (regex/keyword match)
  - Nếu có: ghi motif + evidence quote
  - Nếu phát hiện motif mới: đánh dấu, đề xuất, gán confidence thấp
Bước 3: Tone annotation:
  - Gán tone label: narrative | dialogue | descriptive | introspective
  - Gán emotional tone: neutral | humorous | tense | dreamlike | absurd
Bước 4: Implicit meaning (chỉ nếu rõ ràng):
  - Ghi ẩn ý nếu có bằng chứng cụ thể
  - Null nếu không có
```

### 6.3. MQM-style Quality Annotation (D5)

Dùng cho injected-error set và human evaluation sample.

```
MQM ERROR TAXONOMY (rút gọn):
Accuracy:
  - omission: thiếu ý từ source
  - addition: thêm ý không có trong source
  - mistranslation: sai nghĩa cơ bản

Fluency:
  - grammar_error: lỗi ngữ pháp
  - punctuation_error: lỗi dấu câu
  - unnatural_wording: diễn đạt không tự nhiên

Terminology:
  - wrong_term: dịch sai thuật ngữ
  - inconsistent_term: cùng từ dịch khác nhau

Style:
  - register_inappropriate: ngữ cảnh/formality không phù hợp
  - tone_inconsistent: giọng văn thay đổi bất thường
  - narrative_tone_mismatch: giọng kể không phù hợp văn bản

Severity scale:
  - critical: làm sai nghĩa, mất thông tin quan trọng
  - major: ảnh hưởng đáng kể đến nghĩa hoặc đọc
  - minor: ảnh hưởng nhỏ
  - neutral: stylistic preference
```

### 6.4. MHP Protocol

```
MHP (Monolingual Human Preference) Protocol:
1. Reviewer KHÔNG xem source text.
2. Đọc 2 bản dịch A và B (đã ẩn tên hệ thống).
3. Chọn: A tốt hơn | B tốt hơn | Tương đương.
4. Điều kiện chọn "tương đương": cả hai đều đọc được, không có lỗi rõ ràng.
5. Lý do: ghi 1-2 câu ngắn.
6. Đánh giá độc lập, không thảo luận trước.
7. Minimum reviewers: 3.
8. Thứ tự A/B được random mỗi round.
```

### 6.5. Anchor examples cho Likert narrative quality

```
ANCHOR EXAMPLES — Likert Narrative Quality Scale (1-5):

Score 1 — Rất kém:
"Alice nhanh chóng mở mắt ra. Cô bé nhìn thấy một con thỏ màu trắng.
Con thỏ có đôi mắt hồng."
(Giải thích: Dịch đúng nghĩa từng câu nhưng vụng về, không có giọng kể)

Score 3 — Trung bình:
"Alice vừa mở mắt đã thấy một con thỏ trắng hồng hết nhìn.
Nó đang vội vàng lắm."
(Giải thích: Dịch đọc được, có cố gắng tự nhiên nhưng chưa nhất quán)

Score 5 — Xuất sắc:
"Kìa! Alice vừa chớp mắt một cái, bỗng thấy ngay trước mặt
là con Thỏ Trắng — đôi mắt hồng hoe như thể khóc nhè —
đang hấp tấp chạy ngược."
(Giải thích: Đọc tự nhiên như văn kể tiếng Việt, giữ nhịp, có sáng tạo
ở chỗ "hấp tấp chạy ngược" thay vì "chạy đi", "khóc nhè" cho 
"pink-eyed")

LƯU Ý: Anchor examples KHÔNG lấy từ tập evaluation chính.
```

### 6.6. Inter-annotator Agreement

| Annotation type | Metric | Target |
|-----------------|--------|--------|
| Term annotation (source→target) | Cohen's κ | ≥ 0.70 |
| Entity canonical target | Cohen's κ | ≥ 0.75 |
| Motif occurrence | Cohen's κ | ≥ 0.60 (cho narrative, 0.60 đã khá) |
| MQM severity (injected errors) | Cohen's κ | ≥ 0.65 |
| MHP preference | Fleiss' κ | ≥ 0.40 |

**Nếu agreement thấp:**
- Term/entity: discuss với annotator thứ hai, update guideline, re-annotate 20%.
- Motif: Motif annotation có tính chủ quan cao — giảm target xuống 0.50, ghi rõ limitation.
- MHP: đây là expected. Tính majority vote, phân tích định tính các case gây bất đồng.

---

## 7. Làm sạch và chuẩn hóa

### 7.1. Whitespace và line break

| Tình huống | Xử lý |
|-----------|--------|
| Multiple spaces | Collapse → single space |
| Tab characters | Replace → single space |
| Leading/trailing spaces | Strip |
| Line break trong paragraph | Giữ nguyên `\n` làm semantic marker |
| Line break = paragraph boundary | Replace `\n\n+` → blank line marker |
| Line break trong dialogue | Giữ nguyên `\n` |

### 7.2. Hyphenation

```
QUY TẮC XỬ LÝ HYPHENATION:

Case 1: Word-wrapping hyphen (xuống dòng)
  "被打-\n断" → "被打断"
  Detection: hyphen cuối dòng + dòng tiếp bắt đầu = continuation
  Rule: remove hyphen, merge words

Case 2: Semantic hyphen (có nghĩa)
  "x-axis" → giữ nguyên
  "mother-in-law" → giữ nguyên
  Rule: tra từ điển hyphen compounds

Case 3: Em-dash / en-dash
  "Alice — she thought — was..." → giữ nguyên làm dialogue/narrative marker
  "well-known" → giữ nguyên
```

### 7.3. Quotes và Dialogue

```
QUY TẮC QUOTE:
1. Chuẩn hóa opening/closing quotes:
   - `"` và `"` (curly) → giữ nguyên
   - `` '' '' '' '' '' '' '' '' → flatten → `"`
   - «» (French) → flatten → `"`
2. Không dịch nội dung trong quotes — giữ nguyên làm speaker text.
3. Đánh dấu speaker attribution: "Alice said" → speaker = "Alice", tag = attribution.
```

### 7.4. Footnote, Header, Footer, Page number

| Loại | Xử lý | Lưu lại? |
|------|--------|----------|
| Page number | Detect via regex `[0-9]+` trên dòng riêng → bỏ | Không |
| Header (chapter name) | Detect via position + repetition → bỏ | metadata.chapter_title |
| Footer (publisher) | Detect via position → bỏ | Không |
| Footnote text | Extract nếu có nội dung → gắn tag `footnote` | Có, nếu chứa term/entity |
| Running header | Bỏ | Không |

### 7.5. OCR noise (scanned PDF)

```
OCR POST-PROCESSING:
1. Confidence-based filtering:
   - Block có avg confidence < 0.7 → flag `needs_manual_review`
2. Common OCR patterns:
   - "rn" → "m" (ví dụ: "turn1ng" → "turning")
   - "vv" → "w"
   - "0" (zero) → "O" trong từ thường (giữ nguyên trong số)
   - Fix bằng `pyenchant` dictionary check
3. Whitespace artifacts:
   - Random `\x00`, `\x1a`, multi-space → normalize
4. Manual review cho:
   - Block đã flagged
   - Block chứa proper nouns
   - Block chứa công thức toán
```

### 7.6. Formula và ký hiệu toán

```
XỬ LÝ FORMULA:
1. Detection:
   - Inline: `$...$`, `\(...\)`, hoặc text trong `\[\]`  
   - Block: dòng riêng, có số, ký hiệu toán
2. Extraction:
   - Giữ nguyên text của formula, không parse
   - Đánh dấu: `block_type = "formula"` hoặc `formula_spans`
3. Translation:
   - Tên hàm, biến: giữ nguyên (không dịch)
   - Comment/ghi chú trong formula: dịch bình thường
   - Nếu formula có bug OCR → flag `needs_manual_review`
4. Preservation check (CriticAgent Tier 1):
   - target chứa đủ tokens từ source formula không?
   - Nếu thiếu → `formula_preservation` check fail
```

### 7.7. Quality flag cho extraction

```json
{
  "is_quality_flagged": true,
  "flag_reason": "possible_ocr_error_low_confidence",
  "flag_date": "2026-02-01",
  "reviewed": false,
  "review_note": null
}
```

### 7.8. Dataset versioning sau correction

```
VERSION SCHEME:
vMAJOR.MINOR.PATCH

MAJOR: Thay đổi cấu trúc schema (ví dụ: thêm trường mới)
  → Đổi tên thư mục, cập nhật schema_version

MINOR: Thêm document mới, thêm chapter, thêm annotation layer
  → Cập nhật thư mục con, ghi CHANGELOG

PATCH: Sửa lỗi typo, fix extraction error, cập nhật term/entity annotation
  → Cập nhật file cụ thể, ghi diff log

Ví dụ:
v1.0.0 — Initial release (Alice 3 chương)
v1.1.0 — Thêm 9 chương còn lại
v1.1.1 — Sửa OCR error ở chương 7, cập nhật term annotation
```

---

## 8. Ground Truth và Reference Translation

### 8.1. Khi nào cần reference translation?

| Trường hợp | Cần reference? | Lý do |
|-----------|---------------|--------|
| D1 — sentence-level | **Bắt buộc** | chrF/COMET/GEMBA-DA yêu cầu reference |
| D2 — literary | **Không bắt buộc** | MHP/preference đo hay hơn; reference có thể bias |
| D3 — technical | **Có thể** | Dùng để đo chrF phụ; có thể bỏ qua |
| Phân tích định tính | **20-30 passages** | Chỉ cần subset nhỏ, dùng trong narrative quality discussion |

**Không nên reference translation cho toàn bộ D2** vì:
1. Một bản dịch không đại diện cho tất cả bản dịch tốt.
2. Reference tạo anchor bias — reviewer ngầm so sánh với nó.
3. TRANSAGENTS đã chứng minh: d-BLEU thấp nhưng preference cao hơn reference.

### 8.2. Reference subset nhỏ (20-30 passages)

Chọn 20-30 passages từ D2:
- 10 passages chứa dialogue (Alice nói chuyện)
- 10 passages rich in motif/narrative (Alice thay đổi kích thước, hội trà)
- 5-10 passages kỹ thuật từ D3 (OpenStax)

Dịch thủ công bởi người nghiên cứu. Có thể nhờ 1 reviewer đọc và góp ý.

### 8.3. Rủi ro data contamination với Alice in Wonderland

**Thực tế:**
- Alice có ít nhất 3 bản dịch tiếng Việt phổ biến:
  - Bản Đặng Thế Bính (NXB Kim Đồng)
  - Bản Phùng Hữu Phú
  - Bản dịch online trên various websites
- LLM có thể đã thấy các bản dịch này trong dữ liệu huấn luyện.

**Ảnh hưởng:**
- S0 (baseline) có thể produce bản dịch giống bản có sẵn → TAR/ECS cao hơn thực tế.
- Không đo được genuine memory benefit vì LLM "nhớ" thay vì "truy xuất".

**Giải pháp:**

```
CONTAMINATION MITIGATION:
1. Không dùng bản dịch có sẵn làm reference.
2. Chọn đoạn văn không phổ biến trong Alice (ví dụ: chương 5-6, 
   phần ít được trích dẫn) cho MHP evaluation.
3. Kiểm tra bằng cách: translate cùng một passage bằng S0 2 lần
   với model khác nhau. Nếu cả hai đều giống nhau → có thể có contamination.
4. Ghi rõ trong Chương 4: "Alice in Wonderland có bản dịch Việt ngữ 
   phổ biến. Metric trên D2 có thể bị optimistic bias."
5. Cross-check: thêm 1-2 truyện ngắn ít phổ biến hơn (ví dụ: 
   "The Yellow Wallpaper" của Charlotte Perkins Gilman — public domain, 
   ít bản dịch Việt hơn Alice) làm D2b.
```

### 8.4. Tạo manual reference tối thiểu

```
QUY TRÌNH TẠO REFERENCE NHỎ (20-30 passages):
1. Chọn passages stratified: dialogue, narrative-rich, chapter-opening.
2. Người nghiên cứu dịch lần 1.
3. Để qua đêm, dịch lại lần 2 (không xem lần 1).
4. So sánh 2 bản, chọn bản tốt hơn hoặc tổng hợp.
5. Nhờ 1 reviewer đọc và góp ý (không bắt buộc).
6. Không cần reference cho toàn bộ — chỉ dùng trong phân tích định tính.
```

---

## 9. Evaluation Mapping

### 9.1. Dataset × Metric matrix

| Dataset | Metric | Cách tính |
|---------|--------|----------|
| **D1** | chrF | `sacrebleu.metrics.ChrF()` với reference từ FLORES/PhoMT |
| **D1** | COMET | `unbabel-comet` với model `wmt22-comet-da` |
| **D1** | GEMBA-DA | LLM-as-judge (GPT-4o) với prompt DA style |
| **D2** | MHP | Human preference (3-5 reviewers, majority vote) |
| **D2** | Likert narrative quality | Reviewer chấm 1-5 có anchor examples |
| **D2** | MQM | Human annotator gắn tags trên sample passages |
| **D2** | ECS | Entity Consistency Score (xem công thức bên dưới) |
| **D2** | MATTR/MTLD | `lexicalrichness` library trên toàn bộ VI output |
| **D3** | TAR | Term Accuracy Rate (xem công thức bên dưới) |
| **D3** | ECS | Entity Consistency Score |
| **D3** | Formula preservation | CriticAgent Tier 1 rule check |
| **D4** | TAR | = D4 ground truth matched vs total occurrences |
| **D4** | ECS | = D4 entity consistency matched vs total entity refs |
| **D5** | Precision | (correctly detected by CriticAgent) / (total flagged) |
| **D5** | Recall | (correctly detected by CriticAgent) / (total injected) |
| **D5** | F1 | 2 × P × R / (P + R) |
| **D6** | Recall@K | Relevant memories in top K / total relevant |
| **D6** | MRR | Mean Reciprocal Rank of first relevant |
| **D6** | NDCG | Normalized Discounted Cumulative Gain |
| **All** | Cost | Tính từ token log × bảng giá model |
| **All** | Latency | ms per block translation |
| **All** | Memory Hit Rate | Blocks with non-empty memory retrieval / total |

### 9.2. Công thức TAR và ECS

```
Term Accuracy Rate (TAR):
TAR = (số term được dịch đúng theo D4 ground truth)
      ÷ (tổng số term occurrences trong D4)
      × 100%

Đúng = target chứa expected_target HOẶC allowed_variant
Sai = target chứa forbidden_variant HOẶC bỏ qua

Entity Consistency Score (ECS):
ECS = (số entity reference nhất quán)
      ÷ (tổng số entity references trong document)
      × 100%

Nhất quán = entity được dịch = canonical_target ĐÚNG
            HOẶC = alias_allowed ĐÚNG
            HOẶC = giữ nguyên (nếu không có quy ước)
Không nhất quán = entity được dịch = WRONG form
                  HOẶC = có ≥2 dạng khác nhau cho cùng entity trong 1 chapter
```

---

## 10. Quy mô MVP hợp lý

### 10.1. Chi tiết theo nhóm

| Nhóm | MVP | Full | Ghi chú |
|-------|-----|------|---------|
| **D1** | 300 câu FLORES-200/ PhoMT | 1k-2k câu | Chỉ dùng để đo chrF/COMET baseline |
| **D2** | 3 chương Alice (ch01-ch03), ~60-80 blocks | Thêm ch02-ch07 hoặc thêm truyện khác | Blocks = paragraph-level segmentation |
| **D3** | 1 chương OpenStax CS, ~5k words, ~40-60 blocks | 3-5 chương, ~10k-15k words | Ưu tiên chương có nhiều thuật ngữ lặp |
| **D4** | 50 term pairs + 20 entities | 100 term + 30 entities | Annotate từ D2 + D3 |
| **D5** | 50 injected errors (10 mỗi type × 5 types) | 100 injected errors | Từ output đã dịch của D2 + D3 |
| **D6** | 50 retrieval queries | 100 queries | Từ D2 blocks |
| **MHP pairs** | 30 pairs | 50 pairs | Mỗi pair = 2 bản dịch S3 vs S3d |

### 10.2. Số documents

| Dataset | MVP | Full |
|---------|-----|------|
| D1 | 1 file (300 câu) | 1 file (1k-2k câu) |
| D2 | 1 document (Alice, 3 chương) | 1-2 documents |
| D3 | 1 document (OpenStax, 1 chương) | 1 document (3-5 chương) |

### 10.3. Ước tính thời gian annotate

| Annotation task | Người | MVP (ước tính) | Full (ước tính) |
|---------------|-------|----------------|-----------------|
| Term/entity annotation (D4) | 1 người + 1 checker | 8-12 giờ | 20-30 giờ |
| Motif/tone annotation (D2) | 1 người | 6-10 giờ | 15-20 giờ |
| Injected errors (D5) | 1 người | 4-6 giờ | 8-12 giờ |
| Retrieval queries (D6) | 1 người | 3-5 giờ | 6-10 giờ |
| Quality check / MHP annotation | 1-3 reviewers | 3-6 giờ/reviewer | 3-6 giờ/reviewer |
| **Tổng MVP** | | **~25-35 giờ người** | |

### 10.4. Baseline reference có sẵn

| Nguồn | Số câu | License | Dùng cho |
|--------|---------|---------|---------|
| FLORES-200 dev/test | 1012 EN→VI | CC BY-NC 4.0 | D1 |
| PhoMT subset | 500 EN→VI | CC BY-NC-SA | D1 backup |
| IWSLT'15 EN-VI | 1550 từng cặp | Research only | D1 backup |

---

## 11. Tiêu chí dataset đạt chuẩn

### 11.1. Completeness

```
□ Mọi block đều có `source_text_clean` (không null, không empty)
□ Mọi chapter đều có `chapter_title`
□ Mọi block đều có `block_type`
□ Mọi block đều có `block_id` theo quy ước đặt tên
□ Mọi document đều có `metadata.json` đầy đủ
□ Không có orphan blocks (block thuộc chapter không tồn tại)
□ Dialogue blocks đều có speaker nếu extract được
```

### 11.2. Cleanliness

```
□ Không còn page number trong source_text_clean
□ Không còn header/footer trong source_text_clean
□ Không còn OCR noise (verify bằng sampling 20 blocks)
□ Whitespace đã normalize
□ Hyphenation đã xử lý (verify bằng grep "-\n")
□ Formula giữ nguyên, không bị word-wrapped lỗi
□ Empty blocks đã remove hoặc merge
□ Quality-flagged blocks đã review
```

### 11.3. Consistency

```
□ Block ID naming convention thống nhất: {doc}_{ch}_{b}
□ Chapter ID thống nhất: ch01, ch02, ...
□ Entity canonical names thống nhất xuyên suốt document
□ Term expected targets thống nhất (không 2 cách viết khác nhau)
□ Motif labels thống nhất (dùng motif seed list)
□ JSON schema version nhất quán trong cùng document
□ Annotation confidence đã recorded cho mọi D4 entries
```

### 11.4. Traceability

```
□ Mỗi document có extraction_date và extraction_version
□ Mỗi annotation có annotated_by và annotation_date
□ Nguồn gốc dataset (Project Gutenberg, OpenStax) ghi rõ URL + license
□ Mọi sửa đổi có ghi log (CHANGELOG hoặc diff)
□ Các block chất lượng thấp đã flagged và có review note
```

### 11.5. Reproducibility

```
□ Schema documented (file này đủ để tái tạo)
□ Tool versions ghi trong metadata (pdfplumber 0.10.x, spaCy 3.7.x)
□ Extraction parameters ghi trong metadata
□ Mọi seed lists (motifs, entity) có trong repo
□ Không random seed không documented
```

### 11.6. Licensing

```
□ Mọi source document có license rõ ràng
□ Public domain: ghi năm hết hạn bản quyền
□ CC license: ghi license name + version + URL
□ Không dùng document không có license hoặc license không rõ
□ Nếu dùng excerpt > 10% document: kiểm tra fair use
□ Ghi license của dataset output (nên CC BY 4.0 cho research)
```

### 11.7. No Leakage

```
□ Training/evaluation split (nếu có): không leak evaluation data vào training
□ Trong benchmark D2: tất cả 5 systems (S0/S1/S2/S3/S3d) dịch 
  TRÊN CÙNG source blocks → so sánh fair, không có data leak
□ MHP evaluation: reviewer không biết đang so sánh hệ nào
□ BLEU/COMET: chỉ chạy trên D1 hoặc reference subset, không tính trên D2
□ Prompt không chứa example output từ các hệ khác
```

### 11.8. Versioning

```
□ Mỗi version có tag rõ: v1.0.0, v1.1.0
□ CHANGELOG ghi thay đổi giữa versions
□ Schema version ghi trong JSON header
□ Không modify file sau khi release version
□ Backup / archive folder cho các phiên bản cũ
```

### 11.9. Auditability

```
□ Code extraction có trong repo (không chỉ output)
□ Annotation guideline có trong repo
□ Inter-annotator agreement scores đo và ghi lại
□ Pilot test (10-20 blocks) đã chạy trước khi annotate full
□ Pilot test results có báo cáo ngắn (có thể đưa vào Chương 4)
```

---

## 12. Rủi ro và phản biện

### 12.1. Dataset quá nhỏ

**Vấn đề:**
- Statistical power không đủ → không phát hiện được difference có thật.
- Ví dụ: nếu D2 chỉ có 20 blocks, S3 vs S3d không đủ sample để so sánh có ý nghĩa.

**Ngưỡng tối thiểu:**
- D2: tối thiểu 50 blocks (khoảng 2-3 chương Alice)
- D3: tối thiểu 30 blocks có term/entity occurrences
- D4: tối thiểu 30 term pairs + 10 entities
- D5: tối thiểu 30 injected errors (6 mỗi type)
- MHP: tối thiểu 20 pairs, 3 reviewers

**Giải pháp:** Nếu MVP không đạt ngưỡng → giảm ambition về statistical significance, tập trung qualitative analysis.

### 12.2. Dataset quá sạch

**Vấn đề:**
- Dataset lý tưởng hóa (clean PDF, no OCR noise) không phản ánh real-world.
- Hệ thống hoạt động tốt trên sạch nhưng thất bại trên noisy data.
- Đặc biệt: một số PDF thực tế có layout phức tạp, bảng, formula, page break.

**Giải pháp:**
- Giữ 1-2 noisy samples trong D3 để test robustness.
- Ghi rõ trong Chương 4: "Dataset được clean trước khi benchmark. Real-world performance có thể khác."

### 12.3. Data contamination với Alice

**Vấn đề:** Như đã phân tích ở §8.3.

**Giải pháp:**
1. Không dùng Alice làm **duy nhất** dataset cho D2.
2. Cross-check với D2b = 1 truyện ngắn ít phổ biến hơn.
3. Ghi rõ trong limitations.
4. Kiểm tra contamination bằng cross-model translation (xem §8.3).

### 12.4. Annotation narrative chủ quan

**Vấn đề:**
- Motif, tone, implicit meaning là khái niệm chủ quan.
- Hai annotator có thể gắn nhãn khác nhau.
- Không có ground truth "đúng" cho narrative annotation.

**Giải pháp:**
1. Dùng human-seeded motif list — không fully automatic.
2. Đo inter-annotator agreement và báo cáo.
3. Motif annotation dùng cho **retrieval evaluation** (D6) chứ không dùng cho **quality scoring**.
4. Narrative quality chính được đo bằng MHP (human preference), không bằng motif annotation.

### 12.5. Human evaluation agreement thấp

**Vấn đề:**
- MHP preference có Fleiss' κ thấp (0.11-0.17 trong TRANSAGENTS).
- Likert narrative quality có thể có high variance.

**Giải pháp:**
1. Minimum 3 reviewers; 5-10 nếu có thể.
2. Anchor examples trước khi đánh giá.
3. Đo agreement và báo cáo: "Fleiss' κ = 0.X, agreement = Y%".
4. Nếu agreement quá thấp (< 0.20): phân tích định tính các case bất đồng, ghi rõ trong Chương 4.
5. Không dùng MHP làm **metric duy nhất** cho RQ5 — luôn kết hợp Likert + BLP + MATTR.

### 12.6. Nếu S3 không hơn S3d

**Đây là kịch bản cần chuẩn bị tinh thần:**

```
PHÂN TÍCH NGƯỢC (NẾU S3 ≈ S3d VỀ NARRATIVE QUALITY):

Bước 1: Kiểm tra retrieval quality (D6)
  → Vector retrieval có lấy đúng narrative context không?
  → Nếu D6 Recall@K thấp → retrieval là vấn đề, không phải hypothesis sai

Bước 2: Kiểm tra Interpretation Brief quality
  → Brief có ngắn và hữu ích không?
  → Nếu brief quá generic → Narrative Agent prompt cần tune

Bước 3: Kiểm tra prompt của S3d
  → S3d prompt có đang cheat không? (tự tạo brief ngầm?)
  → Đảm bảo S3d prompt rõ ràng "không dùng narrative understanding"

Bước 4: Kiểm tra dataset
  → D2 có đủ rich narrative để tạo difference không?
  → Nếu blocks quá ngắn / ít motif → dataset không đủ sensitive

Bước 5: Kết luận
  → Nếu mọi bước trên đều OK → narrative agent có thể không cải thiện
    trong điều kiện EN-VI cụ thể này → báo cáo là negative result
  → Negative result vẫn là kết quả nghiên cứu có giá trị
  → Luận văn vẫn có đóng góp: C1 (hybrid retrieval), C2 (summary memory), 
    C4 (CriticAgent)
```

---

## Tổng kết

Dataset suite cho hướng nghiên cứu này gồm **6 thành phần** (D1-D6), phục vụ **5 câu hỏi nghiên cứu** (RQ1-RQ5) với **10+ metrics**.

**Điểm thiết kế then chốt:**

1. **Không gộp tất cả vào một dataset** — mỗi thành phần phục vụ một mục đích đo lường khác nhau.
2. **D2 (literary) là dataset khó nhất** — cần giàu motif, dialogue và emotional tone để đo được narrative quality difference.
3. **Motif annotation: human-seeded, không fully automatic** — giảm noise và tăng ground truth quality.
4. **Reference translation: nhỏ và có chọn lọc** — 20-30 passages, không full document, chủ yếu dùng cho qualitative analysis.
5. **Alice contamination: có thật, cần ghi nhận** — cross-check với D2b (truyện ít phổ biến hơn).
6. **MVP đủ để bảo vệ** với ~60-80 blocks D2 + ~40 blocks D3 + ground truth cho ~50 terms + 50 injected errors.

File này là thiết kế. Việc thu thập, annotate và chạy benchmark thực tế được thực hiện theo lộ trình trong `RESEARCH_PLAN_V3.md` (giai đoạn T2 và T13-T16).
