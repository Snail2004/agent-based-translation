# RETRIEVAL / CONTEXT ARCHITECTURE — Agent-Based Long-Document EN→VI Translation

> **Phạm vi:** Đây là **tài liệu định hướng kiến trúc cho LUẬN ÁN** (agent-based long-document EN→VI translation). **KHÔNG phải task của AI-LAB AIL-202** và **KHÔNG phải app/tool**. Tài liệu mô tả *cách lấy context từ memory cho Translator Agent*, chưa implement, chưa sửa code, chưa tạo schema mới.
>
> **Liên quan:** `RESEARCH_PLAN_V3.md` (§5 agents, §11 token budget, §4.1 disambiguation, §9 S3/S3d), `PROMPT_DESIGN.md` (A.3 Interpretation Brief, A.4 S3 Translator), `DATASET_DESIGN.md` (D6 retrieval relevance). Tài liệu này **không thêm agent mới**: chỉ dùng 4 LLM agent đã chốt (Summary, Narrative Understanding, Translator, Critic) + các module **infrastructure** (Hybrid Retriever, Coordinator…). Mọi thành phần "query planner / reranker / coverage checker" dưới đây là **module hạ tầng tất định, KHÔNG phải LLM agent**.

---

## A. Problem framing — vì sao không chọn cực đoan nào

Hai cách làm đối lập, cả hai đều có vấn đề:

- **Global guideline kiểu TRANSAGENTS** (nhồi Glossary + Book Summary + Tone + Style + Target Audience vào *mọi* prompt): nhất quán, đơn giản, **nhưng tốn token tuyến tính theo số block, loãng dần khi văn bản dài, dính "lost in the middle", và tĩnh — không thích ứng theo từng block.** Với văn bản dài/siêu dài, chi phí và độ nhiễu tăng nhanh.
- **Retrieval-only (naive RAG)**: token-hiệu quả, thích ứng, **nhưng dễ thiếu context văn học (ẩn ý, motif, mạch truyện), sai xưng hô, phân mảnh, và chất lượng bị chặn bởi chất lượng retriever.** Mất "toàn cảnh" tác phẩm.

→ **Định hướng: phân tầng, không nhị phân.** Giữ một **lõi global nhỏ luôn có mặt** (giữ toàn cảnh) + **retrieval per-block** cho phần còn lại + **tách hard/soft** + **nén có kiểm soát**. Mục tiêu: *ít token hơn global-full, nhưng đủ context để dịch văn học có mạch, đúng term/entity/xưng hô/tone/motif.*

---

## B. Dataset JSON (offline) vs Runtime memory store (online)

> ⚠️ **OVERRIDE (2026-06-11, xem `THESIS_ARCHITECTURE_LOCK.md` §5.2):** đoạn "seeding
> dataset JSON → memory store" dưới đây viết TRƯỚC Directional Lock (V3 §0, GVHD chốt
> 2026-06-04) và **không còn hiệu lực**. Runtime memory CHỈ do pre-pass agent tự xây từ 0;
> dataset JSON thuần túy là gold EVAL-ONLY (D6/reference), không được nạp vào pipeline.
> Phần còn lại của doc (hard/soft, coverage, logging, D6) vẫn giữ nguyên giá trị.

Phải tách rõ hai "database":

| | Dataset JSON (AI-LAB) | Runtime memory store T1–T7 (luận án) |
|---|---|---|
| Vai trò | **Offline seed / gold** | **Online**, agent ghi/đọc khi dịch |
| Gồm | document/glossary/entities/chapter_summaries/manual_reference_subset | T1 terminology, T2 entity, T3 discourse+narrative, T4 summary+narrative, T5 translation memory, T6 feedback, T7 QA |
| Khi nào dùng | trước khi chạy (seed) + đánh giá (D6 gold) | trong vòng dịch từng block |

**Cầu nối (seeding):** trước khi dịch một tác phẩm, dataset JSON được **nạp vào memory store**: glossary→T1, entities→T2, chapter_summaries→T4, discourse/motif/tone annotations→T3/T4. Cùng dữ liệu đó còn làm **gold cho D6** (đo retrieval). **Lưu ý quan trọng: retrieval xảy ra trên memory store, KHÔNG trên file JSON.** Translator không bao giờ query file dataset.

---

## C. Architecture overview

Luồng xử lý 1 block (mọi thành phần in nghiêng là **infrastructure tất định**, không phải agent):

```
source block
   │
   ▼
[Query Planner (infra)]      ── phát hiện anchor: entity, term, đại từ, speaker, motif keyword, tham chiếu sự kiện
   │
   ▼
[Hybrid Retriever (infra)]   ── 2 kênh song song:
   │   • HARD: exact/structured lookup (T1/T2/T3)
   │   • SOFT: FTS/BM25 + vector (T3/T4/T5)
   ▼
[Reranker / Filter (infra)]  ── rerank soft candidates, dedup, cắt theo token budget
   │
   ▼
[Coverage Checker (infra)]   ── mọi anchor đã có context chưa? thiếu → flag / (tùy chọn) re-retrieve có giới hạn
   │
   ▼
[Context Pack (cấu trúc)]    ── global_core + hard_constraints + soft_context + coverage + log_ref
   │
   ▼
[Narrative Understanding Agent]  ── (chỉ block khó/triggered) nhận soft_context ĐÃ LỌC → Interpretation Brief
   │
   ▼
[Translator Agent]           ── source block + hard_constraints (luật) + compact soft context + Interpretation Brief
   │
   ▼
[CriticAgent]                ── kiểm tra; có thể yêu cầu repair (max_retry=1)
```

Coordinator (infra) điều phối thứ tự; coverage checker có thể **vòng lại retriever một lần có giới hạn** khi thiếu anchor (không lặp tự do để giữ tính tất định/đo được).

---

## D. Always-on global core (luôn nạp, trần token nhỏ)

Lõi global rẻ, **luôn có mặt** để giữ toàn cảnh — chống rủi ro "retrieval làm mất tác phẩm":

| Thành phần | Nguồn | Ghi chú |
|---|---|---|
| Book/đoạn-hiện-tại summary ngắn | T4 | toàn cảnh cốt truyện đến thời điểm hiện tại |
| Glossary/entity **locked/human_verified** liên quan tác phẩm | T1/T2 | hard constraint nền |
| Character cards nhân vật chính | T2/T3 | tên/xưng hô/quan hệ cốt lõi |
| Style + Target audience card ngắn | T4 | giọng văn tổng |

**Trần token gợi ý:** ≤ ~400–600 tokens (nằm trong Level 1 của token budget V3 §11). Đây là phần *không* retrieve động — luôn prefix, nhưng **nhỏ và cô đọng**, khác hẳn nhồi-full kiểu TRANSAGENTS.

---

## E. Hard context — lấy bằng gì, luật ưu tiên

**Gồm:** glossary term, entity + alias, speaker/addressee, pronoun resolution.
**Lấy bằng:** **exact/structured lookup** (T1/T2/T3) — KHÔNG dùng vector cho hard.
**Vai trò trong prompt:** đưa vào dưới dạng **ràng buộc bắt buộc** (constraint), không phải gợi ý.

**Luật ưu tiên (2 tầng):**
1. **Hard đè soft:** khi soft context gợi một cách dịch mâu thuẫn hard constraint → **theo hard**. Vector similarity không được quyền đổi term/xưng hô.
2. **Disambiguation trong chính hard track** (theo V3 §4.1): khi cùng `source_term`/entity có nhiều `target` hợp lệ (vd "queen" = "nữ hoàng" trong Alice vs "quân hậu" trong N-Queens), ưu tiên `status=locked/human_verified` → `chapter_scope` khớp → `domain` khớp → `global` fallback. Nếu vẫn xung đột nội dung → **escalate cho CriticAgent Tier 2 / human review**, không tự đoán.

→ Hard track là **xương sống nhất quán**; mọi thứ khác là phụ trợ.

**Hard ≠ verbatim.** "Hard đè soft" nghĩa là không được **vi phạm mapping** (sai danh tính, dùng `forbidden_variant`), KHÔNG nghĩa là thay mọi mention bằng `canonical_target`/`expected_target`. Alias, đại từ, và lược chủ ngữ được phép nếu vẫn giữ đúng danh tính và quan hệ xưng hô. Rule-checker (Critic Tier 1) phải kiểm "nhất quán danh tính + không vi phạm forbidden", **không** surface-match thô; nếu không, hệ sẽ ép verbatim qua cửa sau và phá văn phong kể chuyện.

---

## F. Soft context — lấy bằng gì, rủi ro

**Gồm:** motif, tone, implicit meaning, narrative notes, similar passages, previous blocks, chapter summary.
**Lấy bằng:** **FTS/BM25** (trùng từ khóa) + **vector/semantic** (motif/tone/ẩn ý/đoạn tương tự). T3/T4/T5.
**Vai trò trong prompt:** **gợi ý** (qua Interpretation Brief), không phải luật.

**Rủi ro riêng của soft track:**
- Vector kéo về đoạn **giống chủ đề nhưng vô quan** (nhiễu) → LLM "sáng tác" liên hệ.
- Embedding **literary EN/VI yếu** → soft track lấy sai → narrative vô dụng (rủi ro lớn nhất của RQ5).
- Soft context **làm loãng hard constraint** nếu nhồi quá nhiều.
- FTS bỏ sót ngữ nghĩa; vector bỏ sót precision → cần rerank/filter.

→ Vì vậy soft luôn **đi qua reranker/filter + token cap**, và **không bao giờ đè hard**.

---

## G. Context Pack shape (đề xuất minh hoạ — chưa phải schema để implement)

Cấu trúc tách bạch hard/soft để Translator và Critic dùng rõ ràng:

```
ContextPack {
  block_id,
  global_core:      { book_summary, style_card, target_audience, locked_terms[], main_characters[] },
  hard_constraints: {
    glossary:  [{ source, target, allowed_variants, forbidden_variants, status }],
    entities:  [{ canonical_source, canonical_target, aliases, pronoun_policy }],
    discourse: { speaker, addressee, pronoun_resolutions[] }
  },
  soft_context: {
    chapter_summary, previous_blocks[], motifs[], narrative_notes[], similar_passages[]
  },
  interpretation_brief: { ... } | null,
  coverage:  { anchors[], covered[], missing[] },
  retrieval_log_ref
}
```

Nguyên tắc: **`hard_constraints` và `soft_context` là hai khối riêng**, không trộn; Translator được chỉ dẫn coi `hard_constraints` là luật, `soft_context`/`brief` là định hướng.

---

## H. Vai trò của Interpretation Brief

- Do **Narrative Understanding Agent** sinh (xem `PROMPT_DESIGN.md` A.3), **chỉ cho block khó/triggered** (Level 3).
- Là **bản nén 150–300 tokens** của soft context: scene, character_state, implicit_meaning, tone, motifs, translation_strategy.
- **Đầu vào của Narrative Agent là soft_context ĐÃ LỌC** (top-k + dedup), không phải raw — để brief không loãng.
- Brief **không thay** hard constraints; nó hướng dẫn *giọng kể & chiến lược dịch*.
- Translator nhận **brief + hard_constraints + compact soft context**, không nhận raw retrieval.

---

## I. Coverage checker — anchor cần kiểm

Mục tiêu: phát hiện **thiếu context TRƯỚC khi dịch sai**. Với mỗi block, liệt kê anchor và kiểm "đã có context chưa":

| Anchor | "Thiếu" nghĩa là | Lỗi nếu bỏ qua |
|---|---|---|
| entity/tên riêng | không có card | tên dịch bất nhất |
| term/named concept | không có trong glossary | sai/thiếu thuật ngữ |
| đại từ | không resolve được về entity | **sai xưng hô** |
| dialogue/speaker | speaker chưa biết | sai người nói |
| motif keyword | không có narrative note liên quan | mất ẩn ý, dịch phẳng |
| tham chiếu sự kiện trước | không có summary/prev block | mất mạch, mâu thuẫn tình tiết |

**Xử lý khi miss:** flag → (tùy chọn) re-retrieve có giới hạn 1 lần → nếu vẫn thiếu, đánh dấu `low_context` để Critic/human chú ý. Coverage là **gate tất định**, không phải LLM tự quyết.

---

## J. Token budget / context levels (theo V3 §11)

| Level | Khi nào | Gồm gì | Token ước lượng |
|---|---|---|---|
| **Level 1 — normal** | block thường | global_core + hard_constraints + 1–2 previous blocks | ~700–1450 input |
| **Level 2 — dialogue/motif/pronoun** | block có hội thoại, đại từ mơ hồ, motif | + chapter summary + character state + (brief nén) | +~450–1000 |
| **Level 3 — difficult/uncertain** | block khó, nhiều entity, Tier-1 báo risk | + similar passages + narrative evidence + Interpretation Brief đầy đủ | +~500–1200 |

**Adaptive vs uniform:** dùng adaptive (theo độ khó) cho **prototype/production** để tiết kiệm token; nhưng cho **benchmark chính (S3 vs S3d, RQ5)** chạy **uniform** (Narrative Agent mọi block) để khử nhiễu do trigger sai (V3 §5.3).

---

## K. Logging schema (bắt buộc — phục vụ D6/RQ5)

Mỗi block dịch phải log đủ để **chẩn đoán** (không có log thì khi S3 ≈ S3d không biết lỗi do retrieval/brief/model):

```
RetrievalLog {
  block_id, system (S2/S3/S3d), context_level,
  query_signals: [anchor đã phát hiện],
  candidates: [{ memory_id, channel: exact|fts|vector, score, selected: bool }],
  dropped_by_budget: [memory_id],
  narrative_brief_generated: bool,
  tokens: { level1, level2, level3, total_input, output },
  translator_memory_refs_used: [...],   // từ META của Translator (PROMPT_DESIGN A.4)
  coverage: { anchors, covered, missing },
  flags: [low_context, hard_conflict_escalated, ...]
}
```

---

## L. D6 retrieval relevance — đo gì

D6 là **bằng chứng intrinsic** cho RQ5 (retrieval *có lấy đúng* context), tách khỏi chất lượng bản dịch:

- **Recall@K / MRR / NDCG@K** trên query block có gold relevant memories (graded 0/1/2).
- **Ablation theo kênh:** exact-only vs +FTS/BM25 vs +vector → chứng minh **vector thêm narrative recall** (lõi của S3 vs S3d).
- **Precision/noise:** retrieval có kéo memory vô quan không.
- **Hard channel completeness:** exact lookup bắt ~100% glossary/entity xuất hiện trong block (gần tất định).
- Kết nối: nếu D6 tốt mà S3 không hơn S3d → lỗi ở brief/model, **không** phải retrieval → negative result hợp lệ.

---

## M. Rủi ro & mitigation

| Rủi ro | Hệ quả | Mitigation |
|---|---|---|
| Vector kéo lệch hard constraint | term/xưng hô bất nhất | Tách 2 kênh; hard đè soft; disambiguation theo status/scope |
| Embedding literary EN/VI yếu | soft track sai → narrative vô dụng | Chốt model embedding sớm (mục N); đo bằng D6 trước khi tin |
| Không log retrieval | không chẩn đoán S3 vs S3d → RQ5 vô nghĩa | Logging schema (mục K) bắt buộc |
| Translator tự query tự do | không tái lập/đo được | Retrieval layer tất định đứng trước |
| Over-retrieval | lost-in-middle + nhiễu + token | reranker/filter + token cap + level budget |
| Under-retrieval | sai xưng hô/term/ẩn ý âm thầm | coverage checker + re-retrieve có giới hạn |
| Mất toàn cảnh tác phẩm | mạch/motif đứt đoạn | always-on global core |
| Nén quá nhiều bậc | mất chi tiết + nhiều LLM call | giới hạn số bậc nén (mục N) |

---

## N. Quyết định còn mở (không cần chốt ngay)

1. **Embedding model cho semantic EN/VI literary**: mô hình nào; encode trên **source-EN** (mono) hay **cross-lingual**; index theo câu/đoạn/block. *(Quyết định quan trọng nhất — chất lượng soft track phụ thuộc hoàn toàn.)*
2. **Reranker**: có dùng cross-encoder / ColBERT (late interaction) để tăng precision không, hay BM25+vector top-k là đủ.
3. **HyDE cho narrative retrieval**: có sinh "hypothetical narrative note" rồi embed để truy xuất motif không (hữu ích khi block khác phong cách với note), chấp nhận rủi ro hallucinate.
4. **Số bậc nén**: retrieve→pack→brief (2 bậc) hay thêm bậc trung gian — cân chi phí LLM vs mất chi tiết.
5. **Coverage gate**: one-shot hay cho re-retrieve loop có giới hạn; ngưỡng "đủ".
6. **Trigger độ khó** (Level 1/2/3): tín hiệu cụ thể (regex/metadata/entity-count/vector-score) — chấp nhận trigger error như limitation.
7. **Kích thước & nội dung chính xác của global core** + trần token.

---

## O. MVP vs Full

**MVP (đủ để chạy S3 và đo RQ5):**
- Hard track: exact/structured lookup (T1/T2/T3).
- Soft track: FTS/BM25 + vector top-k đơn giản.
- Context pack có cấu trúc (hard/soft tách).
- Interpretation Brief cho block triggered (uniform khi benchmark).
- Coverage checker cơ bản (entity/term/pronoun).
- Logging đầy đủ (mục K) + D6 cơ bản (Recall@K/MRR).
- Always-on global core nhỏ.

**Full (nếu đủ thời gian):**
- Reranker (cross-encoder/ColBERT), HyDE cho narrative.
- Adaptive budget có bộ phân loại độ khó.
- Coverage-driven re-retrieval loop.
- D6 đầy đủ (NDCG + ablation theo kênh + precision).
- Motif registry + evidence cho soft context.

---

*Tài liệu định hướng — phần luận án, không phải task AI-LAB. Chưa implement, chưa sửa code/schema. Đề xuất đặt tại `research/agent-based-translation/RETRIEVAL_ARCHITECTURE.md`.*
