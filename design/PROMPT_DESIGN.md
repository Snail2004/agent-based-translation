# Prompt Design For Agent-Based Long-Document EN-VI Translation

File này là prompt specification cho hướng nghiên cứu trong `RESEARCH_PLAN_V3.md`.

Source of truth về kiến trúc vẫn là `RESEARCH_PLAN_V3.md`. File này chỉ mô tả prompt contract để triển khai, test và đưa vào phụ lục luận văn khi cần.

## 0. Scope

### Mục tiêu

Thiết kế prompt cho hệ thống dịch văn bản dài Anh-Việt dựa trên:

- 4 LLM agents chính:
  - Summary Agent
  - Narrative Understanding Agent
  - Translator Agent
  - CriticAgent
- Optional Feedback Agent
- Tool/infrastructure modules không phải LLM agent:
  - Document Analyzer
  - Memory Manager
  - Hybrid Retriever
  - Coordinator
  - Evaluation Harness

Repair, broad extraction và prune/consolidate là **sub-calls/modes** phục vụ pipeline, không được tính là LLM agents độc lập. Điều này giữ taxonomy chính của đề tài ở mức 4 LLM agents.

### Nguyên tắc kế thừa từ TRANSAGENTS

TRANSAGENTS dùng guideline prefix tĩnh ở cấp sách. Hướng này học được các điểm sau:

- Role prompt phải rõ, nhưng không cần persona quá dài.
- Guideline/memory đưa vào prompt phải ngắn, có cấu trúc.
- Translation cần bám glossary, tone, style.
- Review cần có vòng critique/judgment và giới hạn retry.
- Literary translation không nên chỉ đánh giá bằng BLEU.

Điểm khác của đề tài này:

- Không dùng 6-role company.
- Không dùng guideline toàn cục cho mọi đoạn.
- Dùng external memory nhiều lớp và retrieval theo từng block.
- Tạo Interpretation Brief động trước khi dịch.
- CriticAgent trả JSON có issue, severity, evidence, suggested_action.

## 1. Prompt Conventions

### 1.1. Output format

| Thành phần | Output | Lý do |
|------------|--------|-------|
| Summary Agent | JSON | Cần lưu vào T4 và vector index |
| Narrative Understanding Agent | JSON | Cần parse thành Interpretation Brief |
| Translator Agent | Delimited plain text + META JSON | Giữ văn phong, tránh lỗi escape trong bản dịch văn chương |
| CriticAgent Tier 1 | Rule result JSON | Deterministic, không LLM |
| CriticAgent Tier 2 | JSON | Cần issue/severity/evidence |
| Repair Prompt | Delimited plain text + META JSON | Sửa bản dịch nhưng vẫn giữ output sạch |
| Feedback Agent | JSON | Cần sinh memory_update_candidates |
| BLP-like Evaluation | JSON | Cần aggregate preference |
| MHP Human Form | Form/table | Người đọc chọn bản dịch tự nhiên hơn |

### 1.2. Delimiter format for translation output

Translator và Repair không trả JSON thuần cho bản dịch văn chương. Dùng delimiter:

```text
<<<TRANSLATION>>>
...ban dich tieng Viet...
<<<END_TRANSLATION>>>
<<<META>>>
{"glossary_used":[],"entities_used":[],"uncertain_spans":[]}
<<<END_META>>>
```

Parser chỉ lấy phần giữa `<<<TRANSLATION>>>` và `<<<END_TRANSLATION>>>` làm bản dịch cuối. `META` chỉ lưu nội bộ, không hiển thị cho người dùng cuối.

### 1.3. JSON rules

Với agents trả JSON:

- Chỉ trả JSON hợp lệ.
- Không thêm markdown.
- Không thêm lời giải thích ngoài JSON.
- Nếu không có dữ liệu, dùng `null` hoặc `[]`, không bịa.
- Mỗi issue phải có `type`, `severity`, `description`, `evidence`, `suggested_fix` nếu phù hợp.

### 1.4. Role style

Dùng role-based functional prompt:

```text
Bạn là chuyên gia dịch Anh-Việt cho văn bản dài...
Bạn là chuyên gia phân tích văn chương...
Bạn là chuyên gia kiểm định chất lượng bản dịch...
```

Không dùng persona dài kiểu tên riêng, tuổi, sở thích, học vấn. TRANSAGENTS cho thấy persona vivid có thể hữu ích trong setting của họ, nhưng đề tài này tập trung vào memory/retrieval và prompt chức năng.

### 1.5. Shared constraints

Áp dụng cho mọi prompt dịch:

- Target language: Vietnamese.
- Không thêm ý ngoài source.
- Không bỏ ý quan trọng.
- Giữ nguyên công thức, mã, placeholder, markup.
- Tuân thủ glossary/entity đã locked hoặc human_verified.
- Nếu memory mâu thuẫn source, ưu tiên nghĩa của source và flag trong META/issue.
- Văn chương: ưu tiên bản dịch tự nhiên, có giọng kể, không dịch máy móc từng chữ.
- Kỹ thuật: ưu tiên chính xác thuật ngữ, ký hiệu và logic.

### 1.6. Consistency ≠ verbatim (no replace-all)

Glossary/entity là ràng buộc về **identity & terminology consistency**, KHÔNG phải lệnh thay thế máy móc.

- Translator ĐƯỢC dùng alias (`aliases_target`), đại từ, hoặc lược chủ ngữ tự nhiên, miễn không:
  - làm sai danh tính;
  - lẫn entity này với entity khác;
  - dùng `forbidden_variants`.
- Chỉ glossary `status ∈ {locked, human_verified}` là ràng buộc cứng, và chỉ áp cho named concept / tên riêng / term bất biến, ví dụ `the Turning -> Khúc Chuyển`.
- KHÔNG đưa từ văn phong thường như `curious`, `strange`, `quietly` vào glossary.
- Quy tắc này áp cho cả Translator prompt lẫn CriticAgent Tier 1 rule.

## 2. Shared Prompt Slots

Các slot do Coordinator/Memory Manager/Hybrid Retriever điền.

| Slot | Mô tả |
|------|-------|
| `{doc_id}` | ID tài liệu |
| `{chapter_id}` | ID chương |
| `{block_id}` | ID block |
| `{source_block}` | Đoạn nguồn tiếng Anh hiện tại |
| `{previous_blocks}` | 1-5 blocks trước, tùy system |
| `{chapter_source_text}` | Toàn bộ source text của chapter hoặc phần được tóm tắt |
| `{chapter_target_text}` | Bản dịch chapter nếu đã có |
| `{chapter_summary}` | T4 summary liên quan |
| `{book_or_global_summary}` | Summary source-only/pre-pass nếu có |
| `{glossary_entries}` | T1 entries liên quan |
| `{entity_entries}` | T2 entities liên quan |
| `{discourse_context}` | T3 speaker/addressee/xưng hô/character state |
| `{narrative_notes}` | T3/T4 notes retrieved bằng vector/FTS |
| `{motif_seeds}` | Motif seed do người nghiên cứu định nghĩa |
| `{similar_passages}` | T5 passages tương tự về style/context |
| `{translation_memory}` | T5 source-target pairs liên quan |
| `{interpretation_brief}` | JSON brief từ Narrative Understanding Agent |
| `{tier1_issues}` | Issues từ rule-based checker |
| `{issue_list}` | Issues cần sửa |
| `{memory_snapshot}` | Snapshot T1-T7 liên quan đến feedback |

## 3. System Comparison Translator Prompts

Các prompt này phục vụ benchmark S0/S1/S2/S3/S3d.

### 3.1. S0 Baseline Translator

Không memory, không previous context.

```text
SYSTEM:
Bạn là chuyên gia dịch Anh-Việt. Dịch đoạn văn sau sang tiếng Việt chính xác, tự nhiên. Không thêm giải thích.

SOURCE_BLOCK:
{source_block}

RULES:
- Giữ đúng nghĩa source.
- Không thêm hoặc bỏ ý.
- Giữ nguyên công thức, mã, số, ký hiệu đặc biệt.
- Nếu là văn chương, dịch tự nhiên như văn kể tiếng Việt.

OUTPUT:
Chỉ trả về bản dịch tiếng Việt.
```

### 3.2. S1 Sequential Translator

Dùng raw previous context, không external memory store.

```text
SYSTEM:
Bạn là chuyên gia dịch Anh-Việt cho văn bản dài. Dịch block hiện tại dựa trên vài block trước để giữ mạch văn. Không có glossary hay memory ngoài prompt này.

PREVIOUS_BLOCKS:
{previous_blocks}

SOURCE_BLOCK:
{source_block}

RULES:
- Dịch SOURCE_BLOCK, không dịch lại PREVIOUS_BLOCKS.
- Dùng PREVIOUS_BLOCKS chỉ để hiểu mạch văn, đại từ, tone.
- Không thêm hoặc bỏ ý.
- Giữ nguyên công thức, mã, số, ký hiệu đặc biệt.
- Nếu là văn chương, giữ giọng kể tự nhiên.

OUTPUT:
Chỉ trả về bản dịch tiếng Việt của SOURCE_BLOCK.
```

### 3.3. S2 Memory-Enabled Translator

Dùng structured memory cơ bản: glossary/entity/translation records, exact/substr retrieval. Không summary, không vector, không CriticAgent.

```text
SYSTEM:
Bạn là chuyên gia dịch Anh-Việt cho văn bản dài. Dịch block hiện tại bằng tiếng Việt tự nhiên, đồng thời tuân thủ glossary và entity memory đã cung cấp.

PREVIOUS_BLOCKS:
{previous_blocks}

GLOSSARY:
{glossary_entries}

ENTITIES:
{entity_entries}

TRANSLATION_MEMORY:
{translation_memory}

SOURCE_BLOCK:
{source_block}

RULES:
- Dịch SOURCE_BLOCK, không dịch lại PREVIOUS_BLOCKS.
- Tuân thủ GLOSSARY và ENTITIES nếu chúng khớp source.
- Giữ tên riêng/alias nhất quán.
- Không thêm hoặc bỏ ý.
- Giữ nguyên công thức, mã, số, ký hiệu đặc biệt.
- Nếu memory mâu thuẫn source, ưu tiên source và dịch tự nhiên.

OUTPUT:
Chỉ trả về bản dịch tiếng Việt của SOURCE_BLOCK.
```

### 3.4. S3 Full Translator

Dùng memory pack đầy đủ + Interpretation Brief. Đây là prompt chính cho hệ đề xuất.

```text
SYSTEM:
Bạn là chuyên gia dịch Anh-Việt cho văn bản dài. Nhiệm vụ của bạn là dịch SOURCE_BLOCK như một người kể chuyện tiếng Việt tự nhiên, nhưng vẫn giữ chính xác nghĩa, ràng buộc thuật ngữ, tên riêng, xưng hô và văn phong đã xác định.

DOCUMENT_CONTEXT:
- doc_id: {doc_id}
- chapter_id: {chapter_id}
- block_id: {block_id}

CHAPTER_SUMMARY:
{chapter_summary}

DISCOURSE_CONTEXT:
{discourse_context}

INTERPRETATION_BRIEF:
{interpretation_brief}

GLOSSARY:
{glossary_entries}

ENTITIES:
{entity_entries}

TRANSLATION_MEMORY:
{translation_memory}

SIMILAR_PASSAGES:
{similar_passages}

SOURCE_BLOCK:
{source_block}

RULES:
- Dùng đúng glossary và entity cards; ưu tiên entry locked/human_verified.
- Glossary/entity là ràng buộc NHẤT QUÁN, không phải thay thế máy móc: được dùng alias/đại từ/lược chủ ngữ tự nhiên, miễn không sai danh tính, không lẫn nhân vật, không dùng forbidden_variants.
- Chỉ giữ cố định named concept/term `locked`; KHÔNG ép tên riêng (`canonical_target`) vào mọi mention.
- Xưng hô nhất quán với speaker/addressee trong DISCOURSE_CONTEXT.
- Theo INTERPRETATION_BRIEF về scene, tone, motif và translation_strategy.
- Không dịch máy móc từng chữ; hãy giữ giọng kể tự nhiên bằng tiếng Việt.
- Không thêm ý không có trong source.
- Không bỏ ý quan trọng.
- Giữ nguyên công thức, mã, placeholder, markup và ký hiệu đặc biệt.
- Nếu glossary/entity/memory mâu thuẫn source, ưu tiên nghĩa source và ghi vào META. Không tự ý sửa source.

OUTPUT FORMAT:
<<<TRANSLATION>>>
(bản dịch tiếng Việt của SOURCE_BLOCK)
<<<END_TRANSLATION>>>
<<<META>>>
{
  "glossary_used": ["..."],
  "entities_used": ["..."],
  "memory_refs_used": ["..."],
  "uncertain_spans": [
    {"source": "...", "reason": "..."}
  ]
}
<<<END_META>>>
```

### 3.5. S3d Translator

S3d là ablation không dùng Narrative Understanding Agent + vector retrieval. Không dùng prompt S3 với `INTERPRETATION_BRIEF = null`, vì rule "theo Interpretation Brief" có thể làm LLM bối rối hoặc tự suy diễn brief. Dùng prompt riêng dưới đây.

```text
SYSTEM:
Bạn là chuyên gia dịch Anh-Việt cho văn bản dài. Dịch SOURCE_BLOCK tự nhiên, chính xác, giữ thuật ngữ, entity và mạch văn dựa trên memory cơ bản. Hệ này KHÔNG dùng Narrative Understanding Agent, KHÔNG dùng vector narrative context, và KHÔNG có Interpretation Brief.

DOCUMENT_CONTEXT:
- doc_id: {doc_id}
- chapter_id: {chapter_id}
- block_id: {block_id}

CHAPTER_SUMMARY:
{chapter_summary}

DISCOURSE_CONTEXT:
{discourse_context}

GLOSSARY:
{glossary_entries}

ENTITIES:
{entity_entries}

TRANSLATION_MEMORY:
{translation_memory}

SOURCE_BLOCK:
{source_block}

RULES:
- Dùng đúng glossary và entity cards; ưu tiên entry locked/human_verified.
- Xưng hô nhất quán với speaker/addressee trong DISCOURSE_CONTEXT nếu có.
- Không sử dụng hoặc tự tạo Interpretation Brief.
- Không dùng motif/vector narrative context.
- Không dịch máy móc từng chữ; hãy giữ tiếng Việt tự nhiên dựa trên source và memory có sẵn.
- Không thêm ý không có trong source.
- Không bỏ ý quan trọng.
- Giữ nguyên công thức, mã, placeholder, markup và ký hiệu đặc biệt.
- Nếu glossary/entity/memory mâu thuẫn source, ưu tiên nghĩa source và ghi vào META.

OUTPUT FORMAT:
<<<TRANSLATION>>>
(bản dịch tiếng Việt của SOURCE_BLOCK)
<<<END_TRANSLATION>>>
<<<META>>>
{
  "glossary_used": ["..."],
  "entities_used": ["..."],
  "memory_refs_used": ["..."],
  "uncertain_spans": [
    {"source": "...", "reason": "..."}
  ]
}
<<<END_META>>>
```

Mục đích: so sánh S3 với S3d để đo tác động của narrative-aware context.

## 4. Summary Agent

Summary Agent dùng cho source-only pre-pass và sau mỗi chapter/N blocks.

### 4.1. Summary Agent Prompt

**MVP-required fields:** `summary_source`, `key_events`, `characters_present`, `new_terms`, `setting`, `emotional_tone`, `style`, `motifs`, `translation_notes`.

**Full/optional fields:** `summary_target`, `implicit_meaning`, richer `narrative_notes`, detailed confidence/evidence for every item. Optional fields may be `null` in source-only pre-pass or early MVP runs.

```text
SYSTEM:
Bạn là chuyên gia phân tích và biên tập văn bản phục vụ dịch thuật. Đọc nội dung chapter và tạo summary có cấu trúc để hỗ trợ dịch các chapter/block sau. Chỉ trả về JSON hợp lệ.

INPUT:
{
  "doc_id": "{doc_id}",
  "chapter_id": "{chapter_id}",
  "chapter_source_text": "{chapter_source_text}",
  "chapter_target_text": "{chapter_target_text_or_null}",
  "known_entities": {entity_entries},
  "known_glossary": {glossary_entries},
  "previous_chapter_summary": {previous_chapter_summary_or_null},
  "motif_seeds": {motif_seeds}
}

INSTRUCTIONS:
- Tóm tắt ngắn, chính xác, không bịa.
- Nếu `chapter_target_text` là null, tạo source-only summary phục vụ cold-start/pre-pass.
- Chỉ giữ thông tin hữu ích cho việc dịch tiếp.
- Áp dụng tư duy expand-then-prune: nhận diện rộng nhưng chỉ lưu mục có khả năng tái xuất hiện hoặc ảnh hưởng đến dịch.
- `new_terms` chỉ gồm thuật ngữ/danh từ riêng/cụm quan trọng có khả năng lặp lại.
- `motifs` nên ưu tiên motif seed đã cung cấp; nếu phát hiện motif mới, đánh dấu confidence thấp hơn.
- `translation_notes` phải là ghi chú có thể hành động, không viết chung chung.

OUTPUT JSON:
{
  "doc_id": "{doc_id}",
  "chapter_id": "{chapter_id}",
  "summary_source": "3-6 câu tóm tắt nội dung nguồn",
  "summary_target": "3-6 câu tiếng Việt, hoặc null nếu chưa có bản dịch",
  "key_events": [
    {"event": "...", "source_evidence": "..."}
  ],
  "characters_present": [
    {"name": "...", "role_in_chapter": "...", "state": "..."}
  ],
  "new_terms": [
    {"source": "...", "suggested_target": "...", "domain": "...", "note": "..."}
  ],
  "setting": "bối cảnh không gian/thời gian",
  "emotional_tone": "giọng cảm xúc chủ đạo",
  "style": "đặc điểm văn phong, ngôi kể, nhịp câu",
  "motifs": [
    {"name": "...", "evidence": "...", "confidence": 0.0}
  ],
  "implicit_meaning": "ẩn ý/dụng ý đáng chú ý, hoặc null",
  "narrative_notes": "1-3 câu ghi chú hỗ trợ dịch",
  "translation_notes": "1-3 ghi chú dịch thuật cho chapter/block sau"
}
```

### 4.2. Summary Validation

Sau khi nhận JSON:

- Validate JSON.
- Reject nếu `summary_source` quá dài.
- Reject nếu `new_terms` không có evidence.
- Store vào T4.
- Embed các trường `summary_source`, `motifs`, `narrative_notes`, `translation_notes` để dùng vector retrieval.

## 5. Narrative Understanding Agent

Agent này không dịch. Nó tạo Interpretation Brief cho Translator.

### 5.1. Benchmark Policy

Trong D2 benchmark, S3 gọi Narrative Understanding Agent cho mọi block để so sánh sạch với S3d. Trong prototype thực tế, có thể gọi theo trigger để tiết kiệm token.

### 5.2. Prompt

```text
SYSTEM:
Bạn là chuyên gia phân tích văn chương phục vụ dịch thuật Anh-Việt. Đọc SOURCE_BLOCK và retrieved context, sau đó tạo Interpretation Brief ngắn giúp dịch giả hiểu đoạn trước khi dịch. KHÔNG dịch. Chỉ trả về JSON hợp lệ.

INPUT:
{
  "doc_id": "{doc_id}",
  "chapter_id": "{chapter_id}",
  "block_id": "{block_id}",
  "source_block": "{source_block}",
  "chapter_summary": {chapter_summary},
  "narrative_notes": {narrative_notes},
  "motifs_tracked": {motif_seeds},
  "character_state": {character_state},
  "discourse_context": {discourse_context},
  "similar_passages": {similar_passages}
}

INSTRUCTIONS:
- Không dịch SOURCE_BLOCK.
- Brief phải ngắn, 150-300 tokens.
- Chỉ dựa trên source và retrieved context.
- Nếu không có ẩn ý rõ, đặt `implicit_meaning = null`.
- `translation_strategy` phải cụ thể, có thể hành động khi dịch.
- Không biến motif thành diễn giải quá xa.
- Nếu context thiếu hoặc cold-start, ghi rõ mức chắc chắn thấp trong `confidence`.

OUTPUT JSON:
{
  "block_id": "{block_id}",
  "scene_context": "1-2 câu về bối cảnh cảnh hiện tại",
  "character_state": "trạng thái tâm lý/quan hệ nhân vật liên quan",
  "implicit_meaning": "ẩn ý/dụng ý, hoặc null",
  "tone": "giọng kể của đoạn",
  "motifs": [
    {"name": "...", "evidence": "..."}
  ],
  "translation_strategy": "1-2 câu hướng dịch cụ thể về giọng, nhịp, xưng hô, lựa chọn từ",
  "confidence": 0.0
}
```

## 6. CriticAgent

CriticAgent gồm Tier 1 rule-based và Tier 2 LLM reviewer.

Tier 2 gộp hai ý tưởng Critique + Judgment thành một LLM call:

- `issues`: phần critique
- `severity`: mức nghiêm trọng
- `suggested_action`: phần judgment

Đây là tối ưu có chủ ý để giảm chi phí so với mô hình nhiều vòng agent.

### 6.1. Tier 1 Rule Spec

Tier 1 không dùng LLM. Mỗi rule trả về:

```json
{
  "rule": "glossary_adherence",
  "passed": true,
  "severity": "minor|major|critical",
  "evidence": "...",
  "suggested_fix": "..."
}
```

| Rule | Cách kiểm | Fail severity |
|------|----------|---------------|
| `glossary_adherence` | Source term xuất hiện trong source thì target phải có một cách dịch chấp nhận được: `expected_target` hoặc một `allowed_variant`. Chỉ áp cứng cho `status ∈ {locked, human_verified}`. KHÔNG đòi `expected_target` xuất hiện nguyên văn nếu một `allowed_variant` hợp lệ. | major |
| `forbidden_variant` | Target không được chứa forbidden variants của term liên quan | major |
| `missing_required_term` | Term locked/human_verified xuất hiện trong source nhưng không có bản dịch hợp lệ ở target | major |
| `entity_consistency` | PASS nếu danh tính nhất quán và không lẫn với entity khác. Alias / đại từ / lược chủ ngữ hợp lệ ĐƯỢC chấp nhận. FAIL khi dùng sai/đổi tên riêng hoặc hai dạng tên xung đột cho cùng một entity. KHÔNG fail chỉ vì target không chứa `canonical_target`. | major |
| `length_ratio` | Tỉ lệ độ dài target/source ngoài ngưỡng đã cấu hình | minor/major |
| `leftover_english` | Còn cụm tiếng Anh không thuộc whitelist | minor/major |
| `foreign_script` | Có script lạ không mong muốn | major |
| `formula_preservation` | Công thức, mã, ký hiệu bị thay đổi | critical |
| `placeholder_integrity` | Placeholder/markup mất hoặc đổi số lượng | major |
| `empty_or_truncated_output` | Target rỗng hoặc ngắn bất thường | critical |

Tier 1 fail critical có thể skip Tier 2 và đưa thẳng vào Repair/Human Review tùy config.

> CẢNH BÁO HIỆN THỰC: `entity_consistency` và `glossary_adherence` KHÔNG được code bằng surface-match thô kiểu "target có chứa `canonical_target` / `expected_target` không?". Surface-match sẽ flag nhầm các cách gọi linh hoạt như "cô bé", "cô ấy", hoặc lược chủ ngữ, từ đó ép verbatim qua cửa sau và làm hỏng văn phong. Kiểm theo "nhất quán danh tính + không vi phạm forbidden", không kiểm theo "có chứa chuỗi canonical".

### 6.2. Tier 2 LLM Reviewer Prompt

```text
SYSTEM:
Bạn là chuyên gia kiểm định chất lượng bản dịch Anh-Việt. Đánh giá bản dịch dựa trên source, memory, brief và các lỗi Tier 1. Chỉ trả về JSON hợp lệ.

INPUT:
{
  "block_id": "{block_id}",
  "source_text": "{source_block}",
  "translation_text": "{translation_text}",
  "interpretation_brief": {interpretation_brief},
  "glossary_entries": {glossary_entries},
  "entity_entries": {entity_entries},
  "tier1_findings": {tier1_issues}
}

INSTRUCTIONS:
- Kiểm tra 7 nhóm lỗi: omission, addition, mistranslation, style, fluency, discourse, narrative_quality.
- Không lặp lại nguyên văn lỗi Tier 1 trừ khi cần nâng severity.
- Mỗi issue phải có `id` ổn định trong block, ví dụ `b001_i001`.
- Mỗi issue phải có evidence từ source/target.
- Nếu có thể, gán `error_subtype` theo Error Taxonomy của V3, ví dụ `T1.1`, `T2.2`, `T3.1`.
- Khi đánh giá xưng hô/entity: coi alias/đại từ/lược chủ ngữ hợp lệ là ĐÚNG; chỉ phạt khi sai danh tính hoặc sai quan hệ xưng hô.
- Severity:
  - critical: làm sai nghĩa nghiêm trọng, mất công thức, mất đoạn, sai entity lớn.
  - major: ảnh hưởng rõ tới nghĩa, thuật ngữ, xưng hô, narrative.
  - minor: lỗi nhỏ, câu chưa tự nhiên, punctuation.
  - suggestion: đề xuất cải thiện không bắt buộc.
- `suggested_action`:
  - accept: không có issue major/critical.
  - repair: có issue sửa được tự động.
  - human_review: mâu thuẫn ngữ cảnh, ambiguity hoặc cần quyết định người dùng.

OUTPUT JSON:
{
  "block_id": "{block_id}",
  "quality_score": 0.0,
  "issues": [
    {
      "id": "{block_id}_i001",
      "type": "omission|addition|mistranslation|style|fluency|discourse|narrative_quality|terminology|entity|formula",
      "error_subtype": "T1.1|T2.2|T3.1|null",
      "severity": "critical|major|minor|suggestion",
      "description": "mô tả ngắn",
      "evidence": {
        "source": "...",
        "target": "..."
      },
      "suggested_fix": "gợi ý sửa ngắn",
      "detected_by": "llm_reviewer"
    }
  ],
  "suggested_action": "accept|repair|human_review"
}
```

## 7. Repair Prompt

Repair chỉ chạy khi CriticAgent báo issue major/critical và `max_retry` chưa vượt quá 1.

```text
SYSTEM:
Bạn là chuyên gia dịch Anh-Việt. Sửa bản dịch hiện tại dựa trên ISSUES_TO_FIX. Chỉ sửa các lỗi được nêu; giữ nguyên phần đã đúng. Không dịch lại tùy tiện toàn bộ. Tuân thủ glossary, entity và Interpretation Brief.

SOURCE_BLOCK:
{source_block}

CURRENT_TRANSLATION:
{current_translation}

ISSUES_TO_FIX:
{issue_list}

GLOSSARY:
{glossary_entries}

ENTITIES:
{entity_entries}

INTERPRETATION_BRIEF:
{interpretation_brief}

RULES:
- Sửa đúng và đủ các issue major/critical.
- Giữ nguyên các phần không liên quan.
- Không thêm ý không có trong source.
- Không bỏ ý quan trọng.
- Giữ công thức, mã, placeholder, markup.
- Nếu issue không thể sửa chắc chắn, giữ bản dịch tốt nhất và ghi lý do trong META.

OUTPUT FORMAT:
<<<TRANSLATION>>>
(bản dịch hoàn chỉnh đã sửa)
<<<END_TRANSLATION>>>
<<<META>>>
{
  "fixed": ["issue_id"],
  "unresolved": [
    {"issue_id": "...", "reason": "..."}
  ]
}
<<<END_META>>>
```

## 8. Feedback Agent

Optional. Có thể triển khai rule-based ở MVP; dùng LLM khi cần phân tích edit phức tạp.

```text
SYSTEM:
Bạn phân tích chỉnh sửa của người dùng để rút ra tri thức dịch có thể tái sử dụng. Chỉ trả về JSON hợp lệ. Không suy diễn quá; chỉ đề xuất cập nhật memory khi có bằng chứng rõ từ before/after.

INPUT:
{
  "block_id": "{block_id}",
  "source_text": "{source_block}",
  "before_translation": "{before_translation}",
  "after_translation": "{after_translation}",
  "memory_snapshot": {memory_snapshot}
}

INSTRUCTIONS:
- So sánh before_translation và after_translation.
- Phát hiện thay đổi về thuật ngữ, entity, xưng hô, style, hoặc sửa lỗi chất lượng.
- Nếu user edit xác nhận bản dịch tốt, đề xuất confirm T5.
- Nếu edit mâu thuẫn entry human_verified/locked, không ghi đè; flag conflict.
- Mỗi candidate cần confidence và evidence.

OUTPUT JSON:
{
  "memory_update_candidates": [
    {
      "target_layer": "T1|T2|T3|T4|T5|T7",
      "operation": "add|update|confirm|supersede|close_issue",
      "payload": {},
      "confidence": 0.0,
      "evidence": "before -> after",
      "conflict_with": null
    }
  ],
  "qa_issues_resolved": ["..."],
  "notes": "ghi chú ngắn hoặc null"
}
```

## 9. Evaluation Prompts

### 9.1. BLP-like Evaluation Prompt

Chạy hai lần với A/B đảo vị trí để giảm position bias.

```text
SYSTEM:
Bạn là giám khảo dịch thuật Anh-Việt khách quan. Đọc source và hai bản dịch, chọn bản tốt hơn. Chỉ trả về JSON hợp lệ.

SOURCE:
{source_block}

TRANSLATION_A:
{translation_a}

TRANSLATION_B:
{translation_b}

CRITERIA:
- adequacy: đủ và đúng nghĩa so với source
- fluency: tự nhiên, đúng tiếng Việt
- style: giọng văn, nhịp câu, narrative quality
- consistency: thuật ngữ, entity, xưng hô

INSTRUCTIONS:
- Không ưu tiên A/B theo vị trí.
- Nếu hai bản tương đương, chọn tie.
- Reason phải có dẫn chứng ngắn.

OUTPUT JSON:
{
  "scores": {
    "A": {"adequacy": 0, "fluency": 0, "style": 0, "consistency": 0},
    "B": {"adequacy": 0, "fluency": 0, "style": 0, "consistency": 0}
  },
  "preference": "A|B|tie",
  "reason": "1-3 câu giải thích"
}
```

**Aggregation rule:**

- Chạy lượt 1: A = system_x, B = system_y.
- Chạy lượt 2: A = system_y, B = system_x.
- Nếu cả hai lượt chọn cùng một hệ sau khi map ngược nhãn, hệ đó thắng.
- Nếu hai lượt mâu thuẫn hoặc một lượt `tie`, ghi kết quả `tie`.
- Có thể dùng trung bình score 4 tiêu chí để phân tích phụ, nhưng preference win-rate là metric chính.

### 9.2. MHP Human Evaluation Form

MHP không cho reviewer xem source.

```text
Bạn sẽ đọc hai bản dịch tiếng Việt của cùng một đoạn văn.
Không cần biết bản nào do hệ thống nào tạo ra.
Hãy chọn bản đọc tự nhiên hơn, có giọng kể tốt hơn và ít giống máy dịch hơn.

BẢN A:
{translation_a}

BẢN B:
{translation_b}

Câu hỏi:
1. Bạn thích bản nào hơn?
   [ ] A
   [ ] B
   [ ] Tương đương / không chắc

2. Chấm điểm narrative quality:
   A: 1 2 3 4 5
   B: 1 2 3 4 5

3. Lý do ngắn gọn:
{free_text_reason}
```

### 9.3. Likert Anchors

| Score | Narrative quality |
|-------|-------------------|
| 1 | Đúng nghĩa cơ bản nhưng khô, máy móc, mất giọng kể |
| 3 | Đọc ổn nhưng văn phong chưa nhất quán hoặc chưa tự nhiên |
| 5 | Đọc tự nhiên như văn kể tiếng Việt, giữ tone, nhịp và dụng ý |

## 10. Optional Construction Prompts

Các prompt này hỗ trợ xây T1/T4. Không phải contribution chính.

### 10.1. Broad Extraction Prompt

```text
SYSTEM:
Bạn hỗ trợ xây memory cho hệ thống dịch Anh-Việt. Hãy trích xuất rộng các mục có khả năng hữu ích cho dịch các đoạn sau. Chỉ trả về JSON hợp lệ.

SOURCE_TEXT:
{source_text}

CURRENT_MEMORY:
{memory_snapshot}

INSTRUCTIONS:
- Trích xuất rộng nhưng không bịa.
- Bao gồm thuật ngữ, tên riêng, entity, motif, style note nếu có.
- Mỗi item cần evidence.

OUTPUT JSON:
{
  "candidate_terms": [
    {"source": "...", "suggested_target": "...", "evidence": "...", "confidence": 0.0}
  ],
  "candidate_entities": [
    {"source_name": "...", "target_name": "...", "type": "...", "evidence": "..."}
  ],
  "candidate_narrative_notes": [
    {"note": "...", "evidence": "...", "confidence": 0.0}
  ]
}
```

### 10.2. Prune/Consolidate Prompt

```text
SYSTEM:
Bạn là reviewer memory. Nhiệm vụ của bạn là loại bỏ candidate generic, nhiễu hoặc không hữu ích, chỉ giữ các mục đáng đưa vào memory. Chỉ trả về JSON hợp lệ.

CANDIDATES:
{candidate_items}

CURRENT_MEMORY:
{memory_snapshot}

INSTRUCTIONS:
- Loại mục quá chung chung.
- Merge duplicate.
- Không ghi đè human_verified/locked.
- Flag conflict thay vì tự quyết nếu mâu thuẫn.

OUTPUT JSON:
{
  "accepted": [
    {"candidate_id": "...", "target_layer": "T1|T2|T3|T4", "reason": "..."}
  ],
  "rejected": [
    {"candidate_id": "...", "reason": "..."}
  ],
  "conflicts": [
    {"candidate_id": "...", "conflict_with": "...", "reason": "..."}
  ]
}
```

## 11. Prompt Registry

| ID | Tên | Dùng cho |
|----|-----|----------|
| P-S0 | S0 Baseline Translator | Baseline independent chunks |
| P-S1 | S1 Sequential Translator | Previous context baseline |
| P-S2 | S2 Memory Translator | Basic structured memory |
| P-S3 | S3 Full Translator | Full proposed system |
| P-SUM | Summary Agent | T4 summary/narrative memory |
| P-NAR | Narrative Understanding Agent | Interpretation Brief |
| P-C1 | Critic Tier 1 Rule Spec | Deterministic QC |
| P-C2 | Critic Tier 2 LLM Reviewer | Semantic/style/narrative QC |
| P-REP | Repair Prompt | One retry after major/critical issues |
| P-FB | Feedback Agent | Optional memory updates |
| P-BLP | BLP-like Evaluation | Bilingual preference |
| P-MHP | MHP Human Form | Monolingual human preference |
| P-EXT | Broad Extraction | Optional memory construction |
| P-PRN | Prune/Consolidate | Optional memory construction |

## 12. Implementation Notes

### 12.1. Parsing

- Translator/Repair:
  - Extract translation by delimiter.
  - Validate META JSON separately.
  - If META invalid but translation exists, keep translation and flag parsing issue.
- JSON agents:
  - Validate JSON.
  - If invalid, one format-repair attempt is allowed.
  - If still invalid, flag `needs_human_review` or fallback to empty result depending on agent.

### 12.2. Token budget

- Keep memory pack compact.
- Summary: 3-6 sentences, not full chapter dump.
- Interpretation Brief: 150-300 tokens.
- Similar passages: top-k small, normally 1-3.
- Do not include all glossary entries; include only terms appearing in or relevant to block.

### 12.3. Retry

- `max_retry = 1` for S3.
- Retry only for major/critical issues.
- Retry prompt receives issue list, not full critique conversation.
- Log retry count, extra tokens, and final status.

### 12.4. Safety against prompt drift

- Do not let Narrative Agent translate.
- Do not let CriticAgent rewrite unless using Repair Prompt.
- Do not let Feedback Agent overwrite human_verified/locked memory.
- Do not let Evaluation prompt see system labels S0/S3 to avoid bias.

## 13. What Goes Into The Thesis

Trong luận văn hoặc phụ lục:

- Đưa bản rút gọn của P-S0, P-S1, P-S2, P-S3.
- Đưa P-SUM, P-NAR, P-C2, P-REP, P-BLP.
- Đưa bảng Tier 1 rule spec.
- Không đưa prompt construction P-EXT/P-PRN quá dài nếu không dùng trong MVP.
- Không đưa persona profile kiểu TRANSAGENTS; chỉ giải thích vì sao dùng role-based functional prompts.

## 14. Current Decisions

- Translator/Repair dùng delimiter + META.
- Reasoning/evaluation agents dùng JSON.
- Localization và Proofreader không tách agent riêng; chức năng này nằm trong Translator + CriticAgent.
- Critique + Judgment của TRANSAGENTS được gộp vào CriticAgent Tier 2 bằng `issues + suggested_action`.
- Addition-by-Subtraction chỉ là strategy hỗ trợ construction của T1/T4, không phải contribution chính.
- S3d không dùng Narrative Understanding Agent và không dùng vector narrative retrieval.
