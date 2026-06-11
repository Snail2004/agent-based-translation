# KẾ HOẠCH NGHIÊN CỨU

## Đề tài: Kiến trúc Dịch máy Anh-Việt cho Văn bản dài dựa trên Tác tử với Hệ thống Bộ nhớ Ngoài

> **Câu chốt:** Đề tài thiết kế và đánh giá một kiến trúc dịch máy Anh-Việt cho văn bản dài dựa trên tác tử, trong đó LLM được điều phối bởi hệ thống bộ nhớ ngoài gồm 7 lớp (terminology, entity, discourse, summary, translation, feedback, QA), với retrieval lai giữa exact lookup và FTS/BM25; tích hợp CriticAgent hai tầng để kiểm tra chất lượng tự động; và cơ chế phản hồi người dùng để cập nhật tri thức dịch cho các đoạn tiếp theo.

---

## 1. Cơ sở lý thuyết

### 1.1. Định vị vấn đề

Dịch văn bản dài (sách, báo cáo, tài liệu kỹ thuật) bằng một LLM đơn lẻ nhận input trả output gặp 4 giới hạn cốt lõi:

| Giới hạn | Mô tả | Ảnh hưởng |
|----------|-------|-----------|
| Context window | Tài liệu dài vượt context, phải cắt chunk | Mất ngữ cảnh xuyên chunk |
| Lost in the middle | LLM yếu ở phần giữa context dài (Liu et al., 2024) | Chapters giữa dịch kém nhất |
| Không có self-correction | Không biết quyết định dịch ở block trước | Nhất quán thuật ngữ = 0 |
| No persistence | Mỗi request là blank slate | Không học được từ feedback |

### 1.2. Agent-based translation

**Định nghĩa:** Agent = hệ thống có khả năng Perceive → Reason → Act → Learn/Remember. LLM đơn lẻ chỉ có 2/4.

```
Architecture:
[Document] → [Segmenter Agent]
              → [Memory Builder Agent]
                → [Context Retriever] ──► [Translation Agent] ──► [CriticAgent]
                                                                    → [Memory Update]
                                                                    → [Human Review]
                                                                    → [Feedback Loop]
```

### 1.3. Hệ thống bộ nhớ 7 lớp

```
┌──────────────────────────────────────────────────────────────┐
│ T1: TERMINOLOGY MEMORY                                        │
│ source_term | target_term | status | confidence | variants   │
│ Lookup: EXACT MATCH (độ chính xác cao)                       │
├──────────────────────────────────────────────────────────────┤
│ T2: ENTITY MEMORY                                            │
│ canonical_name | aliases | gender | role | preferred_forms   │
│ Lookup: EXACT surface match → entity ID                       │
├──────────────────────────────────────────────────────────────┤
│ T3: DISCOURSE MEMORY                                         │
│ speaker_turns | pronoun_resolution | character_relations      │
│ form_of_address | emotional_state | timeline_position         │
│ Lookup: structured query theo current block/chapter           │
├──────────────────────────────────────────────────────────────┤
│ T4: SUMMARY MEMORY (Chapter/Event)                           │
│ key_events | characters_present | new_terms | emotional_tone │
│ Trigger: sau mỗi chapter hoặc N blocks                      │
│ Lookup: đẩy vào context pack khi bắt đầu chapter mới        │
├──────────────────────────────────────────────────────────────┤
│ T5: TRANSLATION MEMORY                                       │
│ source_target_pairs | similarity_hash | verified_flag         │
│ Lookup: BM25 / embedding similarity → đoạn tương tự         │
├──────────────────────────────────────────────────────────────┤
│ T6: FEEDBACK MEMORY                                          │
│ before | after | feedback_type | derived_memory               │
│ → Tự động sinh: glossary mới, entity mới, discourse update  │
├──────────────────────────────────────────────────────────────┤
│ T7: QA MEMORY (Issue Log)                                    │
│ issue_type | severity | detected_by | description | fix_detail│
│ → Dùng đo CriticAgent precision/recall                       │
└──────────────────────────────────────────────────────────────┘
```

### 1.4. Hybrid Retrieval

| Loại dữ liệu | Phương pháp | Lý do |
|-------------|-------------|-------|
| Glossary, Entity | Exact/structured lookup | Cần độ chính xác cao |
| Block, Memory item | FTS5 / BM25 | Tìm context gần nghĩa |
| Translation memory | BM25 / embedding similarity | Tìm đoạn dịch tương tự |
| Chapter summary | Embedding (tùy chọn) | Semantic similarity |

### 1.5. CriticAgent hai tầng

```
TIER 1: Rule-based (fast, deterministic)
├── Glossary adherence: term → expected translation có trong target?
├── Entity consistency: entity name/alias có nhất quán?
├── Length ratio: target/source length ratio > threshold?
├── Foreign script: có ký tự lạ trong output?
└── Formula preservation: công thức có bị dịch sai?

TIER 2: LLM-based (slower, semantic)
├── Omission: có ý nào bị bỏ sót?
├── Addition: có ý nào được thêm không có trong source?
├── Mistranslation: có câu nào sai nghĩa?
└── Style/fluency: có lỗi ngữ pháp/tone Tiếng Việt?
```

---

## 2. Các đóng góp chính (Contributions)

| # | Đóng góp | Mô tả | Trạng thái hiện tại |
|---|---------|-------|---------------------|
| C1 | Hybrid Memory Retrieval | Exact lookup + FTS5/BM25 thay linear scan | Schema có FTS indexes, retrieval dùng substring |
| C2 | Chapter/Event Summary Memory | Pipeline sinh summary sau chapter, đẩy vào context | Schema có, `summary = ""` |
| C3 | CriticAgent hai tầng | Rule-based + LLM reviewer, lưu issue log | Chỉ có risk labels |
| C4 | Feedback → Memory update | Phản hồi người dùng tự cập nhật T1-T7 | Feedback có, update chưa đầy đủ |

---

## 3. Thiết kế hệ thống (System Design)

### 3.1. Kiến trúc tổng thể

```
INPUT (Document: PDF, DOCX, TXT)
│
▼
┌─────────────────────────────────────────────────────────────┐
│ 1. SEGMENTER AGENT                                          │
│    • Tách document → blocks / chapters / scenes             │
│    • Extract metadata: page, type, structure                │
│    • Detect dialogue, formula, table                         │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. MEMORY BUILDER AGENT                                     │
│    • Extract entities (NER-style)                           │
│    • Extract terminology candidates                         │
│    • Identify speaker turns (dialogue)                      │
│    • Build discourse structure                              │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. CONTEXT RETRIEVER                                       │
│    • T1: Glossary exact lookup                             │
│    • T2: Entity surface match                              │
│    • T3: Discourse context (speaker, pronoun)              │
│    • T4: Previous chapter summary                          │
│    • T5: Similar translated blocks (BM25)                  │
│    • T7: Previous QA issues (issue log)                    │
│    Output: Memory Pack cho block hiện tại                  │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. TRANSLATION AGENT                                        │
│    • Nhận: source block + memory pack                      │
│    • Prompt: system prompt với glossary, entity, context    │
│    • Gọi: LLM API (GPT-4o, Claude, Gemini)               │
│    • Output: target translation                             │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. CRITIC AGENT                                             │
│    Tier 1: Rule-based checks (glossary, entity, length)    │
│    Tier 2: LLM reviewer (omission, style, fluency)         │
│    Output: quality_json + issues → T7 QA Memory             │
│    If critical issues → retry translation                  │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 6. MEMORY UPDATE                                                             │
│    • Lưu translation record → T5                                               │
│    • Cập nhật entity (if new mentions found)                                │
│    • Cập nhật glossary (if new terms confirmed)                            │
│    • Cập nhật discourse (speaker, pronoun resolution)                     │
│    • Lưu QA issues → T7                                                     │
│    • Trigger chapter summary (T4) nếu hết chapter                           │
└────────────────────────────┬──────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ 7. HUMAN REVIEW (optional)                                   │
│    • User xem/hiệu chỉnh bản dịch                         │
│    • Feedback → T6 Feedback Memory                           │
│    • Feedback consolidator: cập nhật T1-T5 từ feedback    │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
OUTPUT (Translated Document: PDF, DOCX, JSON overlay)
```

### 3.2. Chapter Summary Pipeline

```
Trigger: Khi dịch xong chapter hoặc mỗi N blocks (configurable)

Input cho summarizer:
├── Các blocks đã dịch trong chapter
├── Entity list hiện tại
├── Glossary mới thêm
├── Speaker turns
└── Previous chapter summary (nếu không phải chapter 1)

LLM Summarizer Prompt:
  "Tạo tóm tắt có cấu trúc cho chapter này:
  {
    chapter_id: ...,
    summary_source: ...,
    summary_target: ...,
    key_events: [...],
    characters_present: [...],
    new_terms_added: [...],
    emotional_tone: ...,
    setting: ...,
    translation_notes: ...
  }"

Output → T4: Summary Memory
Retrieval → đẩy vào context pack của chapter tiếp theo
```

---

## 4. So sánh với codebase hiện tại

> **Nguyên tắc:** Codebase hiện tại tại `C:\work\odl-pdf-demo` là prototype/chứng cứ ban đầu, không phải kiến trúc chuẩn. Mỗi module được đánh giá độc lập.

### 4.1. Thực trạng codebase

```
FTS STORAGE: ĐÃ CÓ
├── blocks_fts      → populated trong _replace_block_index()
├── entities_fts    → populated trong upsert_entity()
└── glossary_fts     → populated trong upsert_glossary_entry()

FTS RETRIEVAL: CHƯA DÙNG
├── find_glossary_entries() → duyệt list + substring "in"
├── find_entities()         → duyệt list + set intersection + substring "in"
└── retriever.py           → gọi 2 hàm trên, không query FTS

→ GAP: FTS được ghi nhưng không được đọc trong retrieval.
```

### 4.2. Bảng đánh giá module

| Module | Codebase hiện tại | Cần làm | Priority |
|--------|-------------------|---------|----------|
| SQLite storage | Tốt, 17 tables đã chuẩn hóa | Giữ nguyên | Không |
| Glossary/Entity storage | Tốt | Giữ nguyên | Không |
| FTS indexes (storage) | Đã populate khi ghi | Giữ nguyên | Không |
| FTS indexes (retrieval) | Chưa dùng, dùng linear scan | Refactor find_* | Cao |
| Chapter/Event summary | Schema có, `summary = ""` | Viết pipeline mới | Cao |
| CriticAgent | Chỉ có risk labels | Viết 2-tier mới | Cao |
| Context pack builder | Cơ bản, cần thêm T4 summary | Mở rộng | Trung bình |
| Feedback consolidator | Cơ bản | Mở rộng T1-T7 | Trung bình |
| PDF/UI | Chỉ là adapter | Giữ nguyên | Không |
| Translation agent | LLM call đã có | Giữ nguyên | Không |

### 4.3. Phân loại work items

```
REUSE (đủ tốt, không cần thay):
├── memory/store.py — SQLite wrapper, transaction, CRUD
├── schemas/memory_store_schema.sql — data model
├── memory/context_pack.py — memory pack building
├── memory/translator.py — LLM API call
└── PDF parsing / page extraction

REFACTOR (cần cải thiện đáng kể):
├── find_glossary_entries() — dùng FTS5 query thay linear scan
├── find_entities() — giữ exact match, thêm FTS fallback
└── active_scene.summary — populate từ summary pipeline

VIẾT MỚI HOÀN TOÀN:
├── chapter_summary_pipeline — LLM summarization + storage
├── event_summary_pipeline — event extraction + storage
├── critic_agent/ — Tier 1 (rules) + Tier 2 (LLM reviewer)
├── translation_agent/ — core orchestration
├── retrieval/ft5.py — FTS5 query layer với BM25 ranking
└── evaluation/harness.py — experiment runner + metrics
```

---

## 5. Nghiên cứu và đánh giá (Research & Evaluation)

### 5.1. Research Questions

**RQ1:** Liệu kiến trúc agent-based với memory system có cải thiện đáng kể tính nhất quán thuật ngữ (terminology consistency) và chất lượng dịch so với phương pháp chunk-based không có memory trong dịch văn bản dài Anh-Việt?

**RQ2:** CriticAgent hai tầng (rule-based + LLM) có thể phát hiện và phân loại được bao nhiêu phần trăm lỗi dịch (omission, mistranslation, inconsistency, style)?

**RQ3:** Chapter summary memory và feedback loop có cải thiện chất lượng dịch cho các đoạn tiếp theo không?

### 5.2. Giả thuyết

| # | Giả thuyết | Đo lường | Baseline kỳ vọng |
|---|-----------|---------|------------------|
| H1 | Glossary/terminology memory giảm lỗi sai thuật ngữ | Term Accuracy Rate (TAR) = correct / total glossary occurrences | S0: ~65% → S3: >85% |
| H2 | Entity/discourse memory cải thiện nhất quán nhân vật | Entity Consistency Score (ECS) = consistent / total entity references | S0: ~70% → S3: >90% |
| H3 | Chapter summary cải thiện context understanding | chrF / COMET score trên boundary blocks | Cải thiện 5-10 điểm |
| H4 | CriticAgent phát hiện >60% lỗi | Precision/Recall trên injected errors | Recall: ~65%, Precision: ~70% |
| H5 | Feedback loop cải thiện downstream blocks | Quality score trong vòng 3 blocks sau feedback | Cải thiện đáng kể |

### 5.3. Hệ thống so sánh

| Hệ | Mô tả |
|----|-------|
| **S0: Baseline** | LLM dịch từng chunk độc lập, không memory, không context |
| **S1: Sequential** | LLM + previous chunk context trong prompt |
| **S2: Memory-enabled** | LLM + memory pack hiện tại (glossary, entity, previous blocks) |
| **S3: Full Agent** | S2 + FTS/BM25 retrieval + Chapter summary + CriticAgent |

### 5.4. Ablation

| Ablation | Mô tả |
|----------|-------|
| **S3a** | S3 không Chapter/Event Summary |
| **S3b** | S3 không CriticAgent |

### 5.5. Datasets

```
Layer 1: Sentence-level (đo BLEU, chrF, COMET)
├── IWSLT'15 EN-VI (ted_test2012) — 1,553 sentences
├── PhoMT dev/test — ~2K sentence pairs
└── FLORES-200 EN-VI — 1,012 sentences
→ Mục đích: đo fluency, accuracy ở mức câu

Layer 2: Document-level (đo consistency)
├── Alice in Wonderland — ~20K words, 12 chapters, dialogue-heavy
├── MCS Mathematics excerpt — ~200K words, technical terms, formulas
└── 1-2 chapters tài liệu kỹ thuật (CS/ML)
→ Mục đích: đo TCS, ECS, context preservation

Layer 3: Term/Entity list (đo glossary adherence)
├── 50-100 pre-defined EN-VI term pairs (expert-curated)
├── 20-30 named entities (characters, places, organizations)
└── Đánh dấu occurrences trong test documents
→ Mục đích: đo Term Accuracy Rate, Entity Consistency Score
```

### 5.6. Metrics chi tiết

```
NHÓM 1: Automatic MT Metrics
├── BLEU: n-gram overlap với reference
├── chrF: character-level F-score (tốt cho Vietnamese)
└── COMET / BERTScore: semantic similarity

NHÓM 2: Consistency Metrics (tự định nghĩa)
├── Term Accuracy Rate (TAR):
│   = (số thuật ngữ dịch đúng theo glossary) / (tổng occurrences) × 100%
├── Entity Consistency Score (ECS):
│   = (references nhất quán) / (tổng references) × 100%
└── Content Preservation Rate:
    = (sentences không thiếu/thừa ý) / (tổng sentences) × 100%

NHÓM 3: CriticAgent Metrics
├── Detection Precision = flagged_correct / total_flagged
├── Detection Recall = flagged_correct / total_real_issues
└── F1 = 2 × Precision × Recall / (Precision + Recall)

NHÓM 4: Human Evaluation
├── MQM (Multidimensional Quality Metrics):
│   ├── Accuracy: mistranslation, omission, addition
│   ├── Fluency: grammar, punctuation, naturalness
│   ├── Terminology: wrong term, inconsistent term
│   ├── Style: register, tone, phrasing
│   └── Consistency: named entity, formatting
├── Preference test: System A vs System B (reviewer chọn)
└── Likert scale: fluency (1-5), accuracy (1-5)

NHÓM 5: Process Metrics
├── Memory Hit Rate (MHR): blocks có non-empty memory pack / total
├── Retrieval Time: ms per query
├── Token Usage: avg tokens per block translation
└── Cost: $ per 1000 tokens
```

### 5.7. Experiment Plan

```
EXPERIMENT 1: Memory Impact on Consistency (RQ1, H1, H2)
─────────────────────────────────────────────────────────
Design:
  • Datasets: Layer 2 (Alice + MCS)
  • Systems: S0, S1, S2, S3
  • Metrics: TAR, ECS, chrF, COMET
  • Analysis: paired t-test, p < 0.05

Expected:
  • TAR: S0 ~65% → S3 >85%
  • ECS: S0 ~70% → S3 >90%
  • chrF/COMET: S3 tốt hơn S0 5-10 điểm

─────────────────────────────────────────────────────────

EXPERIMENT 2: CriticAgent Effectiveness (RQ2, H4)
─────────────────────────────────────────────────────────
Design:
  • Inject 50 known errors vào 50 blocks (10 per type)
  • Error types: omission, mistranslation, term_error, entity_error, style
  • Run CriticAgent (Tier 1 + Tier 2) trên các blocks
  • Measure: Precision, Recall, F1 per error type

Expected:
  • Overall Recall: ~65%
  • Overall Precision: ~70%
  • Tier 2 (LLM) tốt hơn Tier 1 (rule) cho omission/mistranslation
  • Tier 1 (rule) tốt hơn Tier 2 cho term/entity consistency

─────────────────────────────────────────────────────────

EXPERIMENT 3: Chapter Summary Impact (RQ3, H3)
─────────────────────────────────────────────────────────
Design:
  • Compare S3 vs S3a (S3 không summary)
  • Focus: blocks at chapter boundaries và chapters giữa document
  • Metrics: chrF, COMET, human evaluation

Expected:
  • Boundary blocks: cải thiện đáng kể
  • Non-boundary: ít hoặc không cải thiện

─────────────────────────────────────────────────────────

EXPERIMENT 4: Feedback Loop (RQ3, H5)
─────────────────────────────────────────────────────────
Design:
  • S3 translate 50% document
  • User corrects 20 blocks (chunk thứ 2)
  • S3 continue translate 50% còn lại
  • Compare: blocks within 3 blocks of corrections vs far blocks

Expected:
  • Near corrections: quality cải thiện
  • Far blocks: baseline quality
```

---

## 6. Lộ trình thực hiện

```
┌─────────────────────────────────────────────────────────────────┐
│ GIAI ĐOẠN 1: CƠ SỞ LÝ THUYẾT VÀ THIẾT KẾ          (Tuần 1-4) │
├─────────────────────────────────────────────────────────────────┤
│ T1-T2: Nghiên cứu tài liệu                                      │
│   • Đọc và tổng hợp: ReAct, RAG, Document-level MT, MQM         │
│   • Viết Chapter 2: Theoretical Framework (30-40 trang)         │
│   • Vẽ: system architecture diagram, agent workflow diagram       │
│   • Vẽ: memory layers diagram                                  │
│   Output: Chương 2 hoàn chỉnh, architecture diagram            │
├─────────────────────────────────────────────────────────────────┤
│ T3-T4: Thiết kế chi tiết                                        │
│   • Thiết kế: CriticAgent output format, issue taxonomy        │
│   • Thiết kế: Chapter summary prompt và schema                  │
│   • Thiết kế: Experiment harness (input/output format)           │
│   • Chuẩn bị: Dataset Layer 2 (Alice reference, MCS excerpts)    │
│   • Chuẩn bị: Term/entity list (50-100 terms)                  │
│   Output: Design docs, prepared datasets                        │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ GIAI ĐOẠN 2: TRIỂN KHAI CỐT LÕI                   (Tuần 5-12) │
├─────────────────────────────────────────────────────────────────┤
│ T5-T6: CriticAgent (Impact cao nhất)                            │
│   • Tier 1: rule-based checks (glossary, entity, length,        │
│   │   foreign script, formula preservation)                       │
│   • Tier 2: LLM reviewer prompt (omission, addition, style)    │
│   • Output format: quality_json + T7 QA Memory                  │
│   • Integration: gọi sau translation, retry nếu critical       │
│   Output: critic_agent/ module hoạt động                       │
├─────────────────────────────────────────────────────────────────┤
│ T7-T8: Chapter/Event Summary Pipeline                           │
│   • Summary trigger (per chapter, per N blocks)                │
│   • LLM summarizer với structured output                       │
│   • Storage → T4 Summary Memory                               │
│   • Retrieval → đẩy vào context_pack                          │
│   • Update: active_scene.summary populated thay vì ""           │
│   Output: chapter_summary_pipeline/ module                     │
├─────────────────────────────────────────────────────────────────┤
│ T9-T10: FTS Retrieval Layer                                     │
│   • Refactor find_glossary_entries() → FTS5 query              │
│   • Refactor find_entities() → giữ exact + FTS fallback        │
│   • Thêm: BM25 ranking cho blocks_fts retrieval               │
│   • Thêm: similarity retrieval cho T5 Translation Memory        │
│   Output: retrieval/ft5.py, refactored find_*                  │
├─────────────────────────────────────────────────────────────────┤
│ T11-T12: Integration và Testing                                 │
│   • Kết nối: all modules → full pipeline S3                   │
│   • Test: trên sample documents                                │
│   • Debug: memory flow, retrieval quality, critic output         │
│   Output: Working S3 prototype                                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ GIAI ĐOẠN 3: EXPERIMENTS VÀ ĐÁNH GIÁ            (Tuần 13-17)  │
├─────────────────────────────────────────────────────────────────┤
│ T13: Benchmark Systems                                          │
│   • Chạy: S0, S1, S2, S3 trên Layer 2 datasets                │
│   • Thu thập: TAR, ECS, chrF, COMET, processing time           │
│   • Baseline metrics: làm rõ S0, S1, S2 baseline numbers        │
│   Output: Raw experiment results                               │
├─────────────────────────────────────────────────────────────────┤
│ T14: CriticAgent Evaluation                                     │
│   • Inject 50 errors                                           │
│   • Run CriticAgent → measure precision/recall                 │
│   • Ablation: Tier 1 only vs Tier 2 only vs Full               │
│   Output: Precision/Recall/F1 scores                           │
├─────────────────────────────────────────────────────────────────┤
│ T15: Ablation Experiments                                      │
│   • S3a (không summary) vs S3                                 │
│   • S3b (không CriticAgent) vs S3                             │
│   Output: Ablation results                                    │
├─────────────────────────────────────────────────────────────────┤
│ T16: Human Evaluation                                          │
│   • Tuyển 5-10 reviewers                                      │
│   • Mỗi reviewer đánh giá ~50 sentence pairs (S0 vs S3)       │
│   • MQM scoring: accuracy, fluency, terminology, style         │
│   Output: Human evaluation results                             │
├─────────────────────────────────────────────────────────────────┤
│ T17: Statistical Analysis                                       │
│   • Paired t-test cho S0 vs S3 (TAR, ECS, chrF)               │
│   • Significance testing: p-value, effect size                │
│   • Error analysis: breakdown lỗi theo type                    │
│   Output: Statistical significance results                      │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ GIAI ĐOẠN 4: VIẾT LUẬN VĂN                       (Tuần 18-24) │
├─────────────────────────────────────────────────────────────────┤
│ T18-T19: Chương 3 (System Design)                              │
│   • 3.1: System Architecture                                   │
│   • 3.2: Memory System Design (7 layers)                       │
│   • 3.3: Hybrid Retrieval Design                              │
│   • 3.4: CriticAgent Design                                   │
│   • 3.5: Chapter Summary Pipeline                             │
│   • 3.6: Implementation Details                               │
├─────────────────────────────────────────────────────────────────┤
│ T20-T21: Chương 4 (Experiments)                                │
│   • 4.1: Experimental Setup (datasets, systems, metrics)       │
│   • 4.2: Experiment 1: Memory Impact Results                   │
│   • 4.3: Experiment 2: CriticAgent Effectiveness               │
│   • 4.4: Experiment 3: Chapter Summary Impact                 │
│   • 4.5: Ablation Results                                    │
│   • 4.6: Human Evaluation Results                             │
│   • 4.7: Statistical Analysis                                 │
├─────────────────────────────────────────────────────────────────┤
│ T22-T23: Chương 5 (Discussion & Conclusion)                   │
│   • 5.1: Summary of Findings                                  │
│   • 5.2: Contributions                                       │
│   • 5.3: Limitations                                          │
│   • 5.4: Future Work                                          │
├─────────────────────────────────────────────────────────────────┤
│ T24: Hoàn thiện                                               │
│   • Chương 1: Introduction (viết lại sau khi biết KQ)        │
│   • Tóm tắt / Abstract                                        │
│   • References                                                 │
│   • Appendix (sample outputs, prompts)                         │
│   • Rà soát toàn văn, chỉnh sửa cuối                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 7. Nguồn tham khảo chính

```
AGENT THEORY
├── ReAct: Synergizing Reasoning and Acting in Language Models
│   (Yao et al., 2023) — arXiv:2210.03629
├── Tool-Augmented Language Models
│   (Schick et al., 2024) — arXiv:2305.11548
└── A Survey of Large Language Model Agents
    (Xi et al., 2023) — arXiv:2308.03688

MEMORY & RAG
├── Retrieval-Augmented Generation for Knowledge-Intensive NLP
│   (Lewis et al., 2020) — arXiv:2005.11401
├── Hybrid RAG strategies (various, 2024)
└── Memory in Language Models: surveys (various, 2023-2024)

DOCUMENT-LEVEL MT
├── Document-level Neural Machine Translation: A Survey
│   (Maruf et al., 2019) — arXiv:1912.08494
├── A Survey of Context in Neural MT
│   (Bawden & Søgaard, Cambridge, 2020)
└── Lost in the Middle: How Language Models Use Long Contexts
    (Liu et al., 2024) — arXiv:2307.03172

MT EVALUATION
├── BLEU: A Method for Automatic Evaluation of MT
│   (Papineni et al., 2002) — ACL P02-1040
├── chrF: Character n-gram F-score
│   (Popović, 2015) — ACL W15-3049
├── COMET: Neural MT Evaluation
│   (Rei et al., 2020) — EMNLP 2020
└── MQM: Multidimensional Quality Metrics
    (Lommel et al., 2014) — TC 1.6

EN-VI DATASETS
├── IWSLT'15 English-Vietnamese
│   (Stanford NLP Group)
├── PhoMT: Vietnamese-English MT Dataset
│   (Nvidia et al., 2021) — EMNLP 2021
└── FLORES-200 / NLLB
    (NLLB Team, 2022) — arXiv:2207.04672
```

---

## 8. Cấu trúc luận văn dự kiến

```
TRANG BÌA
TRANG PHỤ BÌA
LỜI CẢM ƠN
MỤC LỤC
DANH MỤC HÌNH
DANH MỤC BẢNG
TÓM TẮT / ABSTRACT

CHƯƠNG 1: GIỚI THIỆU
  1.1. Bối cảnh
  1.2. Vấn đề nghiên cứu
  1.3. Mục tiêu nghiên cứu
  1.4. Câu hỏi nghiên cứu và giả thuyết
  1.5. Phạm vi và giới hạn
  1.6. Đóng góp
  1.7. Cấu trúc luận văn

CHƯƠNG 2: CƠ SỞ LÝ THUYẾT
  2.1. Dịch máy thần kinh và hạn chế của LLM đơn lẻ
  2.2. Kiến trúc tác tử (Agent Architecture)
  2.3. Hệ thống bộ nhớ ngoài cho tác tử
  2.4. Truy xuất ngữ cảnh lai (Hybrid Retrieval)
  2.5. Dịch máy cấp độ văn bản (Document-level MT)
  2.6. Đánh giá chất lượng dịch máy
  2.7. Tổng kết chương

CHƯƠNG 3: THIẾT KẾ HỆ THỐNG
  3.1. Tổng quan kiến trúc
  3.2. Hệ thống bộ nhớ bảy lớp
  3.3. Lớp truy xuất lai (Hybrid Retrieval Layer)
  3.4. Tác tử dịch (Translation Agent)
  3.5. Tác tử kiểm tra chất lượng (CriticAgent)
  3.6. Pipeline tóm tắt chương/sự kiện
  3.7. Cơ chế phản hồi và cập nhật bộ nhớ
  3.8. Triển khai chi tiết

CHƯƠNG 4: THỰC NGHIỆM VÀ ĐÁNH GIÁ
  4.1. Thiết lập thực nghiệm
  4.2. Thực nghiệm 1: Tác động của bộ nhớ
  4.3. Thực nghiệm 2: Hiệu quả CriticAgent
  4.4. Thực nghiệm 3: Tác động tóm tắt chương
  4.5. Kết quả Ablation
  4.6. Đánh giá con người
  4.7. Phân tích thống kê
  4.8. Phân tích lỗi

CHƯƠNG 5: KẾT LUẬN VÀ HƯỚNG PHÁT TRIỂN
  5.1. Tóm tắt kết quả
  5.2. Đóng góp của luận văn
  5.3. Hạn chế
  5.4. Hướng nghiên cứu tương lai

TÀI LIỆU THAM KHẢO

PHỤ LỤC
  A. Prompt templates
  B. Sample outputs (S0 vs S3)
  C. Term/entity lists
  D. User evaluation form
```

---

## 9. Phụ lục: Error Taxonomy cho CriticAgent

```
ERROR TAXONOMY — Phân loại lỗi dịch

┌──────────────────────────────────────────────────────────────┐
│ T1: TERMINOLOGY ERRORS                                      │
│ ├── T1.1: Wrong term — dịch sai, không theo glossary        │
│ ├── T1.2: Inconsistent term — cùng từ dịch khác nhau      │
│ ├── T1.3: Missing term — thuật ngữ đã biết bị bỏ           │
│ └── T1.4: Over-translation — dịch thuật ngữ không nên dịch  │
├──────────────────────────────────────────────────────────────┤
│ T2: ENTITY ERRORS                                           │
│ ├── T2.1: Inconsistent name — tên riêng dịch khác nhau     │
│ ├── T2.2: Wrong pronoun — đại từ không refer đúng entity    │
│ └── T2.3: Wrong title/role — xưng hô không phù hợp          │
├──────────────────────────────────────────────────────────────┤
│ T3: CONTENT ERRORS                                          │
│ ├── T3.1: Omission — thiếu ý từ source                    │
│ ├── T3.2: Addition — thêm ý không có trong source          │
│ ├── T3.3: Mistranslation — sai nghĩa cơ bản                │
│ └── T3.4: Over-translation — dịch quá/ sai biên độ        │
├──────────────────────────────────────────────────────────────┤
│ T4: STYLE/FORMALITY ERRORS                                 │
│ ├── T4.1: Register shift — formal ↔ informal sai           │
│ ├── T4.2: Inconsistent tone — giọng văn thay đổi bất thường│
│ └── T4.3: Unnatural phrasing — câu Tiếng Việt không tự nhiên│
├──────────────────────────────────────────────────────────────┤
│ T5: SPECIAL CONTENT                                        │
│ ├── T5.1: Formula corrupted — công thức toán bị hỏng       │
│ ├── T5.2: OCR error leaked — lỗi OCR từ input vào output   │
│ └── T5.3: Notation changed — ký hiệu bị thay đổi          │
└──────────────────────────────────────────────────────────────┘
```

---

## 10. Checkpoint để review định kỳ

```
Checkpoint 1 (Tuần 4): Cơ sở lý thuyết
  □ Chương 2 draft xong
  □ Architecture diagram hoàn chỉnh
  □ Dataset Layer 2 đã chuẩn bị

Checkpoint 2 (Tuần 8): CriticAgent xong
  □ Tier 1 + Tier 2 hoạt động
  □ Test trên sample documents
  □ Issue log ghi được vào T7

Checkpoint 3 (Tuần 12): Toàn bộ pipeline S3
  □ Tất cả modules kết nối
  □ Chapter summary pipeline hoạt động
  □ FTS retrieval thay thế linear scan
  □ Test E2E trên 1 document

Checkpoint 4 (Tuần 16): Experiments xong
  □ S0/S1/S2/S3 benchmarked
  □ Ablation S3a/S3b done
  □ Human evaluation done
  □ Statistical analysis done

Checkpoint 5 (Tuần 20): Luận văn draft
  □ Chương 3 + 4 draft xong
  □ Results có số cụ thể

Final (Tuần 24): Nộp
  □ Toàn văn hoàn chỉnh
  □ Rà soát lỗi lần cuối
  □ References đầy đủ
```

---

*Kế hoạch này được xây dựng dựa trên phân tích cơ sở lý thuyết, đánh giá codebase hiện tại tại `C:\work\odl-pdf-demo`, và thảo luận giữa Claude và Code X.*
