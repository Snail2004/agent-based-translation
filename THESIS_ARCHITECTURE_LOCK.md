# THESIS ARCHITECTURE LOCK — Sổ quyết định kiến trúc khóa luận

> **Mục đích:** Đây là file ghi lại MỌI QUYẾT ĐỊNH đã chốt trong quá trình thảo luận
> kiến trúc (bắt đầu 2026-06-11). Khi thực hiện khóa luận, **dựa vào file này trước
> tiên** — không cần tìm hiểu lại từ đầu. File được bổ sung dần qua từng phiên thảo luận
> (ghi vào §10 Changelog).
>
> **Quan hệ với các doc khác** (từ 2026-06-11 doc thiết kế nằm trong `design/`, tham khảo trong `reference/`):
> - `design/RESEARCH_PLAN_V3.md` = thiết kế nghiên cứu (RQ, giả thuyết, metric, thí nghiệm chi tiết).
> - `design/RUN_EVAL_SCHEMA.md` = đặc tả lớp run/eval. `design/SCHEMA_AGENT_FILL_POLICY.md` = fill-tier.
> - `design/PROMPT_DESIGN.md` = contract prompt. `design/RETRIEVAL_ARCHITECTURE.md` = retrieval (có OVERRIDE).
> - `reference/TECH_LEAD_REVIEW_SESSION.md` = transcript tham khảo (KHÔNG phải quyết định).
> - Code + sổ công việc: `THESIS_RUNTIME_TOOL/` (`pipeline/`, `tasks/LEDGER.md`).
> - **Thứ tự ưu tiên khi mâu thuẫn:** Directional Lock (V3 §0, GVHD chốt) > file này > V3 chi tiết > các doc còn lại.
>
> **Trạng thái từng mục:** ✅ ĐÃ CHỐT · 🔶 CHỐT MỀM (đổi được nếu có dữ liệu ngược) · ⬜ CHƯA CHỐT (hàng đợi §8)

---

## 0. Ràng buộc bất biến (GVHD — không thương lượng) ✅

1. **Pipeline tự động từ 0:** ném sách thô vào → nhả bản dịch + số liệu. Agent KHÔNG đọc
   bất kỳ kết quả người làm cho cuốn input (không annotation, không bản dịch người).
2. **Gold AI-LAB = EVAL-ONLY**, cách ly tuyệt đối khỏi mọi thứ translator nhìn thấy.
3. **Phải có số liệu minh chứng**, không đánh giá bằng mắt. BLEU yếu ngữ nghĩa → kết hợp
   neural metric + AI chấm (LLM-judge) + consistency metric. Backtranslation = metric phụ.
4. **Ưu tiên trước mắt: kiến trúc dịch end-to-end DỊCH ĐƯỢC ĐÃ**; nhưng mọi thứ phục vụ
   đánh giá sau này (bảng run/eval, context snapshot) phải gài sẵn từ đầu để khỏi retrofit.
5. **Memory dựng bằng whole-book pre-pass rồi FREEZE** trước khi dịch (V1). Critic chỉ sửa
   output, không sửa memory.
6. **Human feedback loop = future work**, không thuộc đóng góp chính.

## 1. Hướng đi & định vị ✅

- Đề tài: **Tiếp cận hệ tác tử trong bài toán dịch máy Anh-Việt cho văn bản dài** —
  hướng **memory/retrieval-centric narrative translation**. PDF/layout chỉ là adapter.
- Khoảng trống so với TRANSAGENTS: external memory nhiều lớp + hybrid retrieval per-block
  + Interpretation Brief động (thay vì guideline tĩnh toàn sách).
- **Phòng thủ câu hỏi "đây là agent hay pipeline RAG?"** (viết sẵn vào Chương 1/3):
  tính agent nằm ở vòng lặp cấp HỆ THỐNG — pre-pass agents tự xây world model (Perceive),
  Narrative Agent suy luận Interpretation Brief (Reason), Translator dịch (Act), Critic
  review → Repair re-act (Review/Act), ghi T5/T7 (Remember). Coordinator code tất định là
  **lựa chọn phương pháp luận có chủ đích** để ablation đo được — không phải thiếu sót.

## 2. Pipeline & thành phần ✅

```
Sách thô (EPUB/TXT)
  → [Code] Document Analyzer: tách chapter/block, gán block_id bất biến
  → PHASE 1 PRE-PASS (whole-book, theo chương rồi merge):
      World Builder Agent (gộp Glossary + Entity + Relation + Summary/Motif
      thành 1 agent / 1 lượt đọc chương — đòn bẩy token)  → T1,T2,T3,T4
  → [Code] Span Resolver: tính offset bằng string-match (LLM KHÔNG đếm offset)
  → ═══ FREEZE T1–T4 ═══
  → PHASE 2 RUNTIME (per block):
      [Code] Query Planner (anchor scan) → Hybrid Retriever (SQLite + Chroma)
      → Reranker + Token Budget → Coverage Checker
      → (S3) Narrative Agent → Interpretation Brief (150–300 tok)
      → Translator (1 API call, deterministic context feeding, trả META)
      → Critic Tier 1 (code, 0 token) → early-exit hoặc Tier 2 (LLM)
      → fail nghiêm trọng → Repair (max 1 retry)
      → ghi translation_runs + memory_packs + qa_issues; output pass → TM
  → Bản dịch + báo cáo số liệu
```

Quyết định thành phần đã chốt:

| Quyết định | Chọn | Lý do chốt |
|---|---|---|
| Coordinator | **Code tất định** (Python), KHÔNG LLM coordinator | Tái lập 100%, 0 token, ablation sạch |
| Translator tool-use | **KHÔNG tự gọi tool**; 1 call/block + META `uncertain_spans` | Tái lập, chi phí; Coverage Checker chặn trước, Critic soi sau |
| Pre-pass agents | **Gộp World Builder** (1 agent đọc chương 1 lần) | Giảm 4× API call; schema output JSON chặt |
| Narrative Agent trigger | **JIT cho prototype** / **UNIFORM cho benchmark** | Tiết kiệm ~70% khi dev; ablation S3 vs S3d phải sạch |
| Critic | **Tier 1 code + Tier 2 LLM, early-exit** | ~50% block pass Tier 1 không tốn token |
| Critic Tier 1 nguyên tắc | **Nhất quán danh tính, KHÔNG surface-match** (PROMPT_DESIGN §1.6) | Surface-match ép verbatim qua cửa sau, hỏng văn phong |
| Rule "số câu EN = số câu VI" | **LOẠI BỎ** | Dịch văn học tách/gộp câu hợp pháp; giữ `length_ratio` mềm |
| Prompt format | Markdown (hướng dẫn) + XML (ranh giới) + JSON (data) + plain (nội dung) | Mỗi loại thông tin 1 format |
| Prompt caching | Phần tĩnh đặt ĐẦU prompt (system + style + main cards) | Cache 80–90% prompt, đòn tiết kiệm lớn nhất |

### 2.1. Agent taxonomy, quyền hạn & điều phối ✅ (chốt 2026-06-11)

**Taxonomy: 2 loại × 2 tầng, đúng 4 LLM agents** (khớp "4 LLM agents chính" của V3):

- LLM Agent = thành phần suy luận, **pure function** (JSON in → JSON out).
- Code Module = thành phần tất định; KHÔNG gọi là agent trong luận văn (V3 nguyên tắc 7).
- Tầng: Pre-pass (A1) / Runtime (A2–A4).

| # | Agent | Tầng | Chức năng | Output |
|---|---|---|---|---|
| A1 | World Builder | Pre-pass | Trích T1/T2(+relations)/T3/T4 — 1 lượt đọc/chương, TUẦN TỰ với registry-so-far nén (tên+id); cuối phase 1 call Consolidation hợp nhất alias/entity | JSON schema 1.5.0 mirror |
| A2 | Narrative Understanding | Runtime (S3) | Nén context mềm → Interpretation Brief 150–300 tok | Brief JSON |
| A3 | Translator | Runtime | Dịch block từ context pack ĐÃ ĐÓNG BĂNG | Bản dịch + META (glossary_used, entities_used, uncertain_spans) |
| A4 | Critic Tier 2 | Runtime (conditional) | Soi ngữ nghĩa/văn phong/omission | Issues JSON (severity, evidence, suggestion) |

Vai "ẩn" (KHÔNG phải agent riêng): **Repair** = Translator gọi lại với feedback Critic
(max 1); **Consolidation** = 1 call World Builder cuối pre-pass; **Judge** = ngoài hệ,
khác provider, chỉ pha eval.

**Nguyên tắc quyền hạn số 1: LLM KHÔNG BAO GIỜ chạm DB.** Mọi đọc/ghi qua Coordinator
(code) + Memory Manager (validate schema, freeze middleware, ghi provenance). Agent test
được bằng fixture, freeze enforce 1 chỗ, mọi I/O log vào `memory_packs`.

| Agent | Được đọc (Coordinator đưa) | Được ghi (Coordinator ghi hộ) | CẤM |
|---|---|---|---|
| World Builder | chapter text, registry nén | T1–T4 (chỉ TRƯỚC freeze) | gold; ghi sau freeze |
| Narrative | context pack, vector hits | log brief vào memory_packs | sửa memory; thấy reference |
| Translator | context pack cuối | translation_runs | tự retrieve; thấy reference/gold; thấy issues block khác |
| Critic T2 | source, draft, hard constraints, uncertain_spans | qa_issues | tự sửa bản dịch; thấy reference |
| Judge | source + outputs (blind, shuffle) | evaluation_runs | mọi thứ runtime |

**State machine per block** (Coordinator; config S0..S3 bật/tắt từng bước):

```
PLAN→RETRIEVE→BUDGET→CHECK (code, 0 token; thiếu anchor → re-retrieve 1 lần → flag low_context)
→ BRIEF (S3; benchmark=uniform)        call #1
→ TRANSLATE (context pack đóng băng)   call #2
→ CRITIC-1 (code rules; pass sạch → PERSIST)
→ CRITIC-2 (nếu Tier1 không sạch HOẶC có uncertain_spans)  call #3
→ REPAIR (critical/major, max 1)       call #4
→ PERSIST: translation_runs + memory_packs(token_breakdown) + qa_issues; pass → embed TM
```
→ 1–4 call/block, điển hình ~2.

**Failure policy ✅:**
- JSON sai schema → re-ask 1 lần kèm lỗi validate → vẫn sai: block `status=failed`, đi tiếp.
- Repair vẫn fail → giữ bản tốt hơn theo Tier 1 score, issue `open` (bản thân nó là số liệu).
- API lỗi/hết quota → backoff + checkpoint; resume theo "block chưa có run record".

**Tool & calling ✅:** 5 tool (lookup_glossary, lookup_entity, vector_search, get_summary,
get_recent_translations) = code module Coordinator gọi, kết quả nhét sẵn vào context pack
(**Deterministic Context Feeding**). Agent KHÔNG có tools param, KHÔNG ReAct loop runtime.
Chỉ dùng **Structured Outputs** ép JSON. Hai van xả tất định thay cho agentic: Coverage
Checker (trước dịch) + META uncertain_spans (sau dịch). Agentic retrieval = V2/future work,
ghi vào Chương 5 như hướng phát triển có chủ đích.

**Code modules (KHÔNG phải agent):** Document Analyzer (ingest + rule code/math
placeholder), Span Resolver, Context Builder 4 tầng (Query Planner / Hybrid Retriever /
Reranker+Budget / Coverage Checker), Coordinator, Critic Tier 1, Memory Manager + freeze
middleware, Evaluation Harness.

### 2.2. Model stack 🔶 (chốt mềm 2026-06-11 — chốt cứng sau pilot)

Bối cảnh: user có chương trình OpenAI free daily usage (250k tokens/ngày dòng lớn
gpt-5.x/o-series; 2.5M tokens/ngày dòng mini/nano) → chi phí translator ≈ 0.

| Vai trò | Model | Ghi chú |
|---|---|---|
| Translator + World Builder + Narrative + Critic Tier 2 | **`gpt-5.4-mini`** (mini mới nhất trong quota), **pin snapshot version** | 1 model cho MỌI call LLM pipeline (V1); quota 2.5M/ngày |
| S-LC baseline | cùng model translator | sách ngắn (~130–180k tok) vừa nguyên cuốn trong 400k context |
| Nâng cấp dự phòng | `gpt-5.4`/`gpt-5.1` (quota 250k/ngày) | CHỈ khi pilot trượt go/no-go; ưu tiên nâng riêng pre-pass, ghi rõ trong setup |
| Judge (GEMBA/MQM) + Backtranslate VI→EN | **Gemini** (AI Studio free tier) — KHÁC provider | tránh family bias "GPT chấm GPT"; validate judge↔human trên sample |
| Embedding | `text-embedding-3-large` | ngoài quota free nhưng ~$0.02/cuốn; ghi model+dim vào `memory_meta` |

**Kỷ luật đi kèm (✅ chốt cứng):**
- Pin model version, CẤM alias `latest`/`gpt-5-chat-latest`; ghi đúng id vào
  `translation_runs.model`; model update giữa chừng = chạy lại experiment đó.
- OpenAI có `seed` + `system_fingerprint` → ghi cả hai (seed = best-effort, không
  đảm bảo determinism tuyệt đối — ghi chú này vào luận văn).
- Lý do chọn model TẦM TRUNG (viết vào Chương 4): ablation so kiến trúc trên cùng
  model → model rẻ không giảm giá trị khoa học; model tầm trung để headroom lớn hơn
  cho memory architecture thể hiện (frontier model tự nó đã nhất quán → delta nhỏ).
- Traffic free = share data với OpenAI → chỉ dùng sách public-domain qua đường này.
- Runner phải throttle theo quota ngày: checkpoint theo `block_id`, resume được
  (`translation_runs` đã thiết kế sẵn). Fallback nếu chương trình free kết thúc:
  giá chuẩn mini (~$0.25/M input) vẫn rẻ, kế hoạch không đổi.

**Go/no-go sau pilot (mini đủ hay phải lên dòng lớn):**
1. JSON/schema fail rate World Builder < 5% sau 1 retry.
2. Precision entity/glossary extraction vs gold AI-LAB (cuốn pilot phải có gold, vd D2) ≥ ~80%.
3. TAR/ECS internal tách được S0 vs S3.
4. Naturalness spot-check: judge chấm ~20 blocks mini vs dòng lớn trên cùng subset.

## 3. SQLite — kiến trúc database đã chốt ✅

Nền: tái dùng `schemas/memory_store_schema.sql` (schema_version 2) — kế thừa thiết kế
field của schema AI-LAB 1.5.0 (đã khóa). "Kế thừa" = tái dùng field design vào runtime
mirror; KHÔNG ghi vào file gold AI-LAB; provenance agent riêng, không nới enum AI-LAB.

### 3.1. Bảng giữ nguyên (REUSE)

| Memory | Bảng | Ghi chú vận hành |
|---|---|---|
| Trục align | `blocks` | PK `block_id` bất biến — MỌI bảng khác FK về đây |
| T1 | `glossary_entries` | đủ allowed/forbidden_variants, scope, confidence |
| T2 | `entities` + `mentions` | `relations_json` NGỪNG dùng → bảng `entity_relations` mới |
| T3 | `speaker_turns` | chỉ điền block có thoại |
| T4 | `memory_items` | memory_type = summary/motif/tone…; `scenes`/`events` để trống V1, không xóa |
| T5 | `translation_records` | bản canonical đang hiển thị (prototype); KHÔNG dùng cho ablation |
| T6 | `human_feedback` | giữ bảng, KHÔNG dùng (future work) |
| Snapshot | `memory_packs` | + cột mới `config` |
| FTS | `blocks_fts`, `entities_fts`, `glossary_fts`, `memory_items_fts` | giữ nguyên |

### 3.2. Delta — 5 bảng mới + 1 cột (additive, không phá prototype)

1. **`translation_runs`** — engine của ablation. 1 hàng = 1 block × 1 config × 1 stage:
   `run_id PK, experiment_id, doc_id, block_id FK, config (S0..S3x/SLC), stage (draft|revised),
   prev_run_id, output_text, model, prompt_version, temperature, seed, cost, latency_ms, created_at`.
   Ràng buộc: trong 1 `experiment_id`, mọi config dùng CÙNG model/temperature/seed.
2. **`evaluation_runs`** — `eval_id PK, run_id FK, scope (block|chapter|book), scope_id,
   metric_name, metric_value, metric_version, reference_id FK nullable, judge_model
   (PHẢI ≠ translator model), judge_rationale, ablation_label, ci_low, ci_high, created_at`.
3. **`reference_eval_only`** — gold cách ly: `reference_id PK, block_id, target_text,
   provenance (ailab_gold|published), leakage_risk, subset_tag`. Bất biến cứng: chỉ pha
   chấm điểm được đọc `target_text`.
4. **`entity_relations`** — mirror sidecar 1.5.0, phục vụ xưng hô VI động:
   `relation_id PK, doc_id, source_entity_id FK, target_entity_id FK, relation_type,
   state_label, valid_from_block_id, valid_to_block_id (null=mở), trigger_event_id,
   address_policy_json (2 chiều độc lập: self_term/address_term), evidence_json, confidence, notes`.
   Precedence runtime: `pronoun_hints > active state (theo order_index) > default relation
   > entity.pronoun_policy > style fallback`.
5. **`qa_issues`** (T7 thành bảng thật — chỉnh quyết định cũ "để trong quality_json là đủ",
   vì E2 cần precision/recall theo hàng issue, đào JSON blob lúc eval = retrofit):
   `issue_id PK, doc_id, run_id FK→translation_runs, block_id FK, tier (tier1|tier2),
   rule_or_subtype (tên rule Tier1 hoặc mã taxonomy V3 §13), severity (minor|major|critical),
   evidence_source, evidence_target, suggestion, fixed INTEGER, retry_count INTEGER, created_at`.
6. **`memory_packs` thêm cột `config`** (S0..S3) + bump `schema_version` 2 → 3.

### 3.3. Quy tắc FREEZE ✅

- Sau pre-pass: ghi `frozen_at` vào `memory_meta`. Middleware từ chối INSERT/UPDATE vào
  T1–T4 (`glossary_entries`, `entities`, `entity_relations`, `mentions`, `speaker_turns`,
  `memory_items`) sau thời điểm này.
- Runtime chỉ được ghi: `translation_runs`, `translation_records`, `memory_packs`,
  `qa_issues`, collection `translation_memory` (Chroma).
- Mọi entry agent sinh: provenance `model/prompt_version/confidence/version`;
  KHÔNG set `human_verified/locked` ở runtime.

## 4. ChromaDB — đã chốt cấu trúc, chưa chốt embedding model

### 4.1. Collections ✅

| Collection | Chứa | Ghi khi nào |
|---|---|---|
| `similar_passages` | source EN của mọi block (1 block = 1 vector) | Pre-pass, cố định |
| `narrative_motifs` | motif/summary/tone notes từ T4 (1 note = 1 vector) | Pre-pass, cố định |
| `translation_memory` | cặp dịch của chính agent, CHỈ bản pass Critic — **embed vế EN làm khóa, VI = payload** (không gian embedding thuần EN, §5.2) | Runtime, append |

- Metadata bắt buộc mỗi vector: `{doc_id, chapter_id, block_id, kind}` — để filter scope.
- Query: top-k 3–5; TM giới hạn scope 3–5 chương gần nhất (metadata filter).
- Chỉ embed bản VI đã pass Critic → chặn lan truyền lỗi qua đường "style reference".
- Ghi `embedding_model` + `dimension` vào `memory_meta` ngay lần index đầu (đổi model =
  re-index toàn bộ).

### 4.2. Embedding model 🔶 CHỐT MỀM (2026-06-11)

- **`text-embedding-3-large`** (OpenAI) — cùng provider với translator (§2.1), cross-lingual
  tốt, có MRL, chi phí ~$0.02/cuốn. KHÔNG bake-off thực nghiệm.
- Ghi `embedding_model` + `dimension` vào `memory_meta` ngay lần index đầu.

## 5. Token & inject policy — chỗ quyết định "đủ mà không thừa" ✅

**Nguyên tắc 3 tầng:** (1) Storage schema rộng rãi = free; (2) Fill-policy quyết định
token SINH (tier A/B/C/D — xem `SCHEMA_AGENT_FILL_POLICY.md`: A code 0đ, B 1 lần/cuốn,
C 1 lần/chương, D per-block CHỈ block khó, mặc định null); (3) **Inject budget quyết
định token DỊCH** — khóa dưới đây.

**Quy tắc chọn context (code tất định, 0 token):** chỉ nhét thứ match **anchor** trong
block — glossary có occurrence trong block, entity card có mention trong block, discourse
chỉ khi có thoại, brief chỉ ở S3. KHÔNG BAO GIỜ dump cả registry.

| Zone (thứ tự trong prompt) | Budget/block | Tính chất |
|---|---|---|
| **ZONE 1 — TĨNH CẢ RUN**: system prompt + output contract + style policy + book synopsis (~150) + top-10 main character cards + hot glossary top-20 one-liner | **~1.100–1.300 tok** (cố tình ≥ ngưỡng cache 1024 — xem §5.1) | Cache ~10% giá |
| **ZONE 2 — BÁN TĨNH THEO CHƯƠNG**: chapter summary + motif notes | ~200 tok | Cache hit trong chương |
| **ZONE 3 — ĐỘNG**: hard constraints anchor-based (≤400) + rolling window 1–2 block VI (~300) + Brief S3 (150–300) + source block (~200–400) | ~700–1.400 tok | Trả full giá |
| **Tổng input/block** | **~2.000–2.900 (S3)**, trong đó trả-full chỉ ~700–1.400 | |

- Mỗi lần đóng pack: ghi `token_breakdown` vào `memory_packs` — vừa là bằng chứng
  context_bundle, vừa là số "chi phí kiến trúc" cho Chương 4.
- Main character cards (GLOBAL core) = top entity theo số `mentions` — derivable, không
  thêm field `importance` mới.

### 5.1. Cơ chế Cache & Compact ✅ (chốt 2026-06-11)

**Luận điểm trả lời GVHD ("mỗi lần gọi là ném lại toàn bộ"):** API stateless là bản chất,
nhưng pipeline KHÔNG gửi tất cả — nó gửi context pack ~2k tok do code chọn tất định, và
kích thước **PHẲNG O(1) theo độ dài sách** (block 1 hay block 1.000 đều ~2k; tri thức dài
hạn nằm trong store, chỉ phần liên quan được lôi ra). So sánh: chat history = O(n);
S-LC = ~100k+/call tăng theo sách. "Agent không quên vì nó không cần nhớ — nó TRA CỨU";
rủi ro duy nhất là retrieval miss, và miss đo được (`low_context`, memory hit rate).

**COMPACT (4 quy tắc):**
1. Mọi memory entry render bằng **template cố định 1–2 dòng**; không bao giờ dump raw.
2. Rolling window = 1–2 block liền trước, **CHỈ bản VI** (mạch văn nằm ở target).
3. Narrative dài hạn KHÔNG gửi raw — vector hits nén qua Brief 150–300 tok (compact có
   suy luận). Chống quên dài hạn (motif ch3 vọng lại ch28).
4. KHÔNG conversation history — mỗi block 1 call độc lập.

**CACHE (OpenAI automatic prompt caching, prefix ≥1024 tok → ~10% giá input):**
- **Nghịch lý chốt:** zone tĩnh GIÀU (1.200 tok, cached ≈ trả 120) RẺ HƠN zone tĩnh gọn
  (600 tok, dưới ngưỡng, trả 600) — vừa rẻ ~5× vừa chở thêm context. → cố tình làm giàu
  Zone 1 đến ~1.100–1.300 tok bằng nội dung ổn định hữu ích nhất.
- Cache theo longest matching prefix → layout 3 zone (bảng trên); block cùng chương hit
  tới hết Zone 2.
- **Kỷ luật byte-identical:** entry sort cố định theo id; template không timestamp/random;
  `prompt_hash` theo zone lưu vào `memory_packs` để audit cache-friendliness.
- TTL cache ngắn (~5–10 phút) → chạy **tuần tự liền mạch theo từng config** (xong S0 cả
  sách mới sang S3), không xen kẽ config.
- Judge Gemini: cùng nguyên tắc — rubric tĩnh đặt đầu (implicit caching).

**Cache code-level (không phụ thuộc provider):**
- **Replay cache:** Coordinator check `(model, prompt_hash, temperature, seed)` trong
  `translation_runs` trước khi gọi API — crash resume / re-run / debug không tốn lại
  token nào. Tiết kiệm lớn nhất ở giai đoạn dev.
- **Embedding cache:** embed theo content-hash, không re-embed text không đổi.

**Reasoning tokens (riêng gpt-5 family):** mặc định model "suy nghĩ" và TÍNH TIỀN như
output → chốt `reasoning_effort: minimal/low` + verbosity low cho Translator (dịch không
phải bài toán logic); cho phép medium với World Builder/Critic nếu pilot cần.

**Số minh họa (1.000 blocks, S3):** naive không cache ~2,4M; static gọn không đạt ngưỡng
~1,8M; **chốt (zone + cache) ~1,35M (−45%)**; S-LC ~50M+ tăng theo sách. Biểu đồ
"token/block phẳng vs S-LC tăng" = hình đắt giá cho Chương 4.

### 5.2. Retrieval — định vị RAG & cơ chế chống thiếu/chống thừa ✅ (chốt 2026-06-11; nền: RETRIEVAL_ARCHITECTURE.md)

**Định vị so với taxonomy RAG** (viết vào Chương 2/3):
- KHÔNG phải Classic RAG / Graph RAG / Agentic RAG thuần — là **"GraphRAG-shaped offline,
  keyed-lookup online"** + hybrid retrieval (C1).
- **Insight trung tâm:** đa số nhu cầu context của dịch KHÔNG phải similarity mà là
  **tra cứu có khóa** — pre-pass + Span Resolver đã đánh chỉ mục mentions/occurrences/
  relations/summaries theo `block_id` → runtime phần lớn = `SELECT ... WHERE block_id=X`
  (P≈1, 0 token, tất định). Vector CHỈ cho phần thực sự mờ (motif, similar passages, tone).
- Classic RAG → chỉ soft track. Graph RAG → lấy data model (`entity_relations` = cạnh có
  pha), BỎ machinery (community/multi-hop); **KHÔNG dùng graph DB** (Neo4j…) — SQLite +
  `valid_from/to` so theo `order_index` đủ cho 1-hop (thêm vào §7 loại trừ). Agentic RAG
  → loại ở runtime; bản có kiểm soát: Narrative Agent diễn giải (không chọn query),
  Coverage re-retrieve = corrective tất định (CRAG-lite).

**Chống THIẾU — 4 lưới recall (rẻ → đắt):**
1. Pre-pass whole-book + span resolve (anchor được TRA từ chỉ mục, không phát hiện runtime).
2. Fallback scan: alias/lemma + FTS/BM25 cho dạng bề mặt pre-pass sót.
3. Coverage Checker gate: re-retrieve scope mở rộng đúng 1 lần → vẫn thiếu → `low_context`
   (tỉ lệ này = metric sức khỏe hệ).
4. META `uncertain_spans` (sau dịch, Critic soi; lặp hệ thống → lỗi registry, vào error analysis).

**Chống THỪA — 5 van precision** (distractor làm LLM lệch + lost-in-middle):
1. Anchor-gated: không anchor → 0 truy vấn.
2. Hard/soft tách kênh + **hard đè soft** (soft không được đổi term/xưng hô; hard ≠ verbatim).
3. **Threshold trước top-k** (khởi điểm cosine ~0.75, calibrate ở pilot) — top-k là TRẦN,
   không phải hạn ngạch phải tiêu.
4. Brief = màng lọc kép: vector hits → rerank/dedup → Narrative Agent nén 150–300 tok;
   Translator không bao giờ thấy raw retrieval.
5. Token cap theo zone + render template 1–2 dòng (§5/§5.1).

**Đo retrieval RIÊNG:** D6 (Recall@K/MRR/NDCG + ablation kênh exact/FTS/vector +
precision-noise) + RetrievalLog (RETRIEVAL_ARCHITECTURE §K: candidates/score/selected/
dropped_by_budget) → tách "lỗi retrieval" khỏi "lỗi translator"; S3 ≈ S3d vẫn chẩn đoán
được; negative result hợp lệ.

**Embedding space & granularity ✅ (đóng câu hỏi mở RETRIEVAL_ARCHITECTURE §N.1):**
- **Thuần EN, không cross-lingual**: query luôn tạo từ source EN của block hiện tại →
  mọi collection khóa bằng EN. `translation_memory` embed **vế EN của cặp dịch**, VI là
  payload trả về → rủi ro cross-lingual bị loại bằng thiết kế.
- Granularity: `similar_passages` = 1 block/vector; `narrative_motifs` = 1 note T4/vector;
  `translation_memory` = 1 cặp dịch/vector (khóa EN).

**Tiêu chí "retrieval ĐỦ" (definition of done, tránh tranh luận cảm tính):**
1. Hard channel bắt ≥99% glossary/entity xuất hiện trong block (tra chỉ mục, gần tất định).
2. `low_context` rate < ~5% số block.
3. Soft track: D6 Recall@5 đạt ngưỡng chấp nhận ở pilot (số cụ thể đặt khi có ~50–100
   relevance queries); trượt → nâng cấp theo danh sách Full (reranker/HyDE,
   RETRIEVAL_ARCHITECTURE §O), không đập kiến trúc.

**Còn nợ (không chặn thiết kế):** calibrate threshold (khởi điểm cosine ~0.75) + top-k
(3–5) ở pilot; reranker chỉ thêm nếu D6 precision kém; D6 relevance queries phải tự tạo;
giả định rủi ro nhất của RQ5 (embedding bắt được motif/tone văn học) chỉ được tin sau
khi D6 có số.

**Override doc cũ (RETRIEVAL_ARCHITECTURE.md):**
1. §B "seeding dataset JSON → memory store" **CHẾT theo Directional Lock §0** (doc viết
   trước 2026-06-04): memory CHỈ do pre-pass tự xây; dataset JSON = gold eval-only.
2. §D global core ≤400–600 tok → thay bằng Zone 1 giàu ~1.100–1.300 tok (§5.1, cache threshold).

## 6. Thiết kế thí nghiệm — khung đã chốt (chi tiết ở V3 §9–§12)

- **Ablation ladder** (định nghĩa chuẩn V3 §9): S0 → S1 → S2 → S3; ablation S3a/S3b/S3d.
  S3c (feedback) = future work. Cùng base model/temperature/seed trong 1 experiment.
- 🔶 **Baseline S-LC (long-context) — bổ sung mới** (Claude đề xuất 2026-06-11, cần trình
  GVHD): dịch theo chương với toàn bộ/phần lớn văn bản trước đó trong context window,
  không memory không agent. Lý do: S0 là baseline yếu ("người rơm") — practitioner 2026
  sẽ nhét cả sách vào context 1M; phải thắng (hoặc đo được trade-off với) S-LC thì luận
  điểm mới vững. Chi phí thêm ≈ 1 config của runner.
- ✅ **Pilot sớm**: dịch 1–2 chương bằng S0 + S3 thô, đo TAR/ECS internal ngay khi runner
  chạy được — kiểm chứng metric TÁCH ĐƯỢC hai hệ trước khi xây hết; có số sơ bộ báo GVHD.
- ✅ **Phân vai metric**: consistency `*_internal` (toàn sách, không cần gold) = chủ lực;
  COMET/COMET-Kiwi + LLM-judge (GEMBA/MQM, judge ≠ translator, blind A/B shuffle) = chất
  lượng ngữ nghĩa; BLEU/chrF = phụ trên reference subset (kèm bootstrap CI);
  backtranslation = diagnostic. Memory quality (`*_gold`) đo RIÊNG translation quality.
- ✅ **Thứ tự ưu tiên khi thiếu thời gian**: S0 → S-LC → S3 → S3d (RQ5, đóng góp riêng)
  → S3a/S3b nếu còn budget. MVP subset 80–120 blocks đại diện cho benchmark; pre-pass
  vẫn chạy toàn sách.

### 6.1. Dataset 2 track — D2L (kỹ thuật) + văn học 🔶 (chốt mềm 2026-06-11)

GVHD chỉ định **D2L — Dive into Deep Learning** (d2l.ai) vì có bản dịch VI cộng đồng
"Đắm mình vào Học Sâu" (vi.d2l.ai, v0.17.5, ~19 chương; bản 0.14.4 tại d2l.aivivn.com
có Bảng thuật ngữ; repo `d2l-ai/d2l-vi`).

| | Track 1: D2L (kỹ thuật) | Track 2: **Treasure Island** (văn học) 🔶 |
|---|---|---|
| Vai trò | **End-to-end + số liệu ĐẦU TIÊN** (báo GVHD) | RQ5 / Narrative Agent / S3-vs-S3d (novelty C2/C3) |
| Reference | d2l-vn = reference VI gần cả sách → BLEU/chrF/COMET diện rộng | **gold AI-LAB tự tạo** (`manual_reference_subset`, n nhỏ + CI); KHÔNG dùng bản dịch xuất bản làm reference chính |
| Gold T1/T2 | **Bảng thuật ngữ d2l-vn = gold đo `tar_gold`** miễn phí | gold AI-LAB: annotation 4–5 chương quanh arc — **ch8 (gặp Silver), ch11 (apple barrel), ch14–15 (đảo, Ben Gunn), ch28 (trại địch — xưng hô flip)** |
| Source | **EN snapshot từ CHÍNH repo d2l-vi** (cùng commit bản VI — EN d2l.ai hiện tại đã khác xa); markdown, align theo cấu trúc file, KHÔNG OCR/PDF | Project Gutenberg plain text, KHÔNG OCR |
| Quy mô chạy | subset chương (sách lớn) | **nguyên cuốn 34 chương ~67k từ ~90k tok** (full S3 run ≈ 1,5–2M tok = 1 ngày quota; S-LC lọt context 400k) |
| Memory phát huy | T1 terminology (TAR delta dễ thấy) | T2/T3/T4 + `entity_relations` 3 tầng — xem lý do dưới |

**Lý do chọn Treasure Island (Claude đề xuất 2026-06-11, thay Alice của V3 D2):**
1. **Jim↔Silver = arc quan hệ đổi pha kinh điển** (thầy thân → apple barrel lộ phản bội
   → thù → đồng minh bất đắc dĩ) → map thẳng vào `entity_relations.state_label` +
   `valid_from_block_id`; xưng hô VI bắt buộc đổi theo pha → **case study định tính đắt
   nhất cho Chương 4** (S0 dịch đều đều, S3 đổi đúng pha).
2. **Dàn nhân vật cố định xuyên 34 chương** (Jim, Silver, Smollett, Livesey, Trelawney,
   Ben Gunn) → entity consistency bị chấm liên tục. Alice bị loại chính vì EPISODIC
   (nhân vật 1–2 chương rồi biến mất, memory ít đất diễn) + wordplay = nhiễu metric.
3. **Alias dày**: Silver = Barbecue = the sea-cook; bẫy disambiguation: **"Captain
   Flint" = hải tặc đã chết VÀ con vẹt**.
4. **T1 giàu**: thuật ngữ hàng hải/hải tặc (schooner, cutlass, the Black Spot…).
5. **POV switch ch16–18** (Jim → Dr. Livesey) = test narrator/discourse hiếm có.
6. Motif: Black Spot, rum & cái chết, "Pieces of eight!".
7. Nhược điểm chấp nhận: nổi tiếng ở VN → leakage (xử lý như D2L: ablation delta +
   memorization test); tiếng Anh TK19 + khẩu ngữ thủy thủ = thử thách dịch chính đáng.
   Hệ quả: motif seeds / prompt samples trong V3 đang viết theo Alice → cập nhật khi chạm tới.
8. Runner-up đã cân nhắc: Peter Pan (quan hệ tĩnh), A Little Princess (ít glossary),
   Oz (quan hệ phẳng), Alice (episodic + puns).

**Cross-check phụ (optional, V3 §10.7):** nếu đủ thời gian, thêm 1 truyện public-domain
ÍT phổ biến ở VN có arc quan hệ tiến triển (ứng viên: *A Little Princess*,
*The Secret Garden*). Chốt ở pha thí nghiệm.

**Quản lý rủi ro D2L (bắt buộc):**
1. **Leakage HIGH** — EN+VI công khai từ 2020, chắc chắn trong training data GPT-5.x.
   Phòng thủ: (a) luận điểm chính = **ablation delta** (leakage tác động đều mọi config);
   (b) `reference_eval_only.leakage_risk = high`, báo cáo kèm caveat; (c) **memorization
   test**: cho model dịch ~10 đoạn, so trùng verbatim với d2l-vn, khai kết quả trong luận văn.
2. **Version pinning** — block_id sinh từ snapshot commit của d2l-vi, không lấy EN mới.
3. **Coverage** — kiểm đếm chương nào dịch hoàn chỉnh trước khi chọn subset (§8 item 3a).
4. **Code/math** — Document Analyzer thêm rule: code block + công thức = placeholder /
   do-not-translate; Critic Tier 1 check placeholder integrity (đã có trong design).

**Finding phụ kỳ vọng:** S3 vs S3d trên cả 2 track → narrative layer giúp văn học,
trung tính trên kỹ thuật = bằng chứng hệ thống hoạt động đúng thiết kế, không ăn may.

### 6.2. Khung chấm điểm benchmark ✅ (chốt 2026-06-11; chi tiết metric gốc ở V3 §11)

**Nguyên tắc: KHÔNG có 1 điểm tổng — chấm bằng PROFILE 4 trục**, mỗi claim 1 metric chủ lực:

| Trục | Metric chủ lực | Thang | Claim phục vụ |
|---|---|---|---|
| 1. Đúng từ | TAR + ECS (`_internal` toàn sách / `_gold` subset) | 0–100% | "hơn dịch thường về nhất quán" |
| 2. Đúng nghĩa | COMET-Kiwi (ref-free, toàn sách) + COMET (subset) + backtranslation (diagnostic) | 0–1 | "không đánh đổi nghĩa" (S3 ≥ S0) |
| 3. Đúng mạch | MQM-lite category discourse + ACS (🔶 optional) | điểm phạt /1k từ | xưng hô/đại từ đúng quan hệ |
| 4. Đúng giọng | Pairwise blind A/B (judge) + GEMBA-DA + MATTR + Human Likert | win-rate % / 0–100 / 1–5 | "văn hay hơn"; S3 vs S3d (RQ5) |

**Định nghĩa khóa (vũ khí chính, phải nhất quán với PROMPT_DESIGN §1.6):**
- **TAR** = occurrence dùng `expected_target` HOẶC `allowed_variant` / tổng occurrence;
  match qua chuẩn hóa lemma + variant list, KHÔNG bắt nguyên văn (nếu không TAR sẽ
  thưởng bản dịch máy móc → tự phản bội đề tài).
- **ECS** = per entity: 1 − entropy chuẩn hóa của các dạng tên đã dùng qua mentions;
  alias trong `aliases_target` KHÔNG bị phạt; trung bình trọng số theo mention count.
- `_internal` = vs registry agent tự xây (self-consistency; S0 chấm bằng registry của
  run S3 cùng experiment cho công bằng). `_gold` = vs gold (extraction correctness).
  Hai trục TÁCH BẠCH (memory quality ≠ translation quality).
- **ACS (Address Consistency Score) 🔶 optional**: per speaker_turn, extract cặp
  xưng–hô VI trong câu dịch (dict đại từ + LLM-extract) → so với `address_policy`
  đang active theo pha. Metric đo trực tiếp entity_relations 3 tầng (arc Jim↔Silver).
  Chỉ làm nếu kịp.

**LLM-judge — 2 chế độ + protocol chống bias (chốt cứng):**
- **MQM-lite** (chẩn đoán): judge đánh lỗi theo taxonomy V3 §13; điểm = 100 − tổng phạt
  /1.000 từ; trọng số **minor=1, major=5, critical=10, non-translation=25** (hệ
  Freitag/WMT). Ra bảng phân loại lỗi cho Chương 4.
- **Pairwise blind A/B** (so sánh): nhạy nhất với văn phong; chọn A/B/hòa + lý do.
- Protocol: judge = Gemini (khác provider), temperature 0, prompt version pin, log
  `judge_rationale`; **mỗi cặp chấm 2 lần đảo vị trí** (mâu thuẫn → hòa); blind tuyệt
  đối (không tên config, không memory/brief; chỉ source + bản dịch + 1 đoạn context);
  **calibrate**: 30–50 block có điểm human → Spearman ρ judge↔human; ρ ≥ 0.5 → judge
  dùng diện rộng, thấp hơn → human lên chính. ρ tự nó là kết quả báo cáo.

**Hai track chấm KHÁC NHAU:**
| | D2L (kỹ thuật) | Treasure Island (văn học) |
|---|---|---|
| Ref-based (BLEU/chrF/COMET) | CÓ Ý NGHĨA (ít biến thể hợp lệ; d2l-vn đáng tin) | CHỈ PHỤ (d-BLEU thấp ≠ dịch tệ — TRANSAGENTS) |
| Trục chủ lực | TAR vs Bảng thuật ngữ d2l-vn + COMET | Pairwise win-rate + ECS/ACS + human Likert |
| Human eval | không cần | BẮT BUỘC (E5: 3–10 người, 30–50 pairs stratified, Likert 1–5 + preference, Fleiss' κ) |

**Backtranslation (đề xuất GVHD) — protocol:** VI→EN bằng Gemini (≠ translator) →
COMET/BERTScore vs source EN. Vai trò: **cờ chẩn đoán per-block** (điểm thấp → soi tay,
vào error analysis) + báo cáo tương quan backtranslation↔judge để định giá phương pháp.
KHÔNG là metric chính (nhiễu kép round-trip).

**Thống kê & lịch chấm:**
- Mọi so sánh config: paired bootstrap trên block scores (cùng block_id) → CI 95% +
  p-value; reference subset n nhỏ bắt buộc kèm CI.
- Pilot: TAR/ECS internal + COMET-Kiwi + GEMBA-DA (đủ biết metric có tách S0/S3).
- Full: + MQM-lite + pairwise mọi cặp ablation + backtranslation.
- Cuối: human round E5 + calibrate judge.
- Chi phí ≈ 0: judge = Gemini free tier; COMET local; sacrebleu/MATTR free.
- `evaluation_runs.metric_name` mở rộng thêm: `mattr`, `acs_internal`, `acs_gold`
  (cột TEXT, additive, không phá schema).

### 6.3. Oracle reference run (CodeX GPT-5.5) 🔶 (ghi nhận 2026-06-12)

User chạy CodeX/GPT-5.5-extra-high (agentic, context 258k) trên tool AI-LAB:
annotation full 40 chương Treasure Island (1.476 blocks, 470 terms, 58 entities,
69 relations đều có pha, validate 0 lỗi, ~1,9M tokens/3,5h) → translation preview
full 40 chương dùng chính annotation đó + book_style_brief.json.

**Vai trò HỢP LỆ:** (a) mỏ neo TRÊN của thang chất lượng (S0 → S3-thesis → oracle);
(b) rehearsal trọn vòng annotate→context→dịch→import; (c) đối thủ pairwise blind
judging vs S3; (d) annotation draft → human review subset arc → gold AI-LAB;
(e) upper-bound reference cho memory-quality (mini vs strong extraction).

**CẤM (chống dùng nhầm):**
1. KHÔNG phải baseline trong ablation ladder (khác model, agentic, không seed,
   không tái lập) — chỉ là reference point.
2. KHÔNG bao giờ thành reference/gold cho metric (đo giống-GPT-5.5 ≠ đo chất lượng).
3. KHÔNG nạp annotation/translation của nó vào thesis runtime (memory phải do
   World Builder pipeline tự sinh — Directional Lock).
4. Caveat bắt buộc khai trong Chương 4: oracle nhiễm bẩn training data (bản dịch
   "Đảo giấu vàng" xuất bản) → cần memorization spot-check ~10 đoạn; oracle dùng
   annotation cùng họ model → so với thesis là so cả cụm, không tách biến.

**Việc ngay khi dịch xong:** đo TAR_internal/ECS_internal trên output oracle bằng
registry sẵn có = những con số metric ĐẦU TIÊN của dự án + kiểm chứng định nghĩa
§6.2 vận hành; ghi cost tokens để vào bảng so sánh chi phí.

## 7. Đã LOẠI khỏi scope (đừng mở lại) ✅

| Thứ bị loại | Lý do |
|---|---|
| Human feedback loop trong core (T6/C5/RQ4/E4/S3c) | Mâu thuẫn autonomous + freeze (GVHD) |
| LLM Coordinator | Mất tái lập, nhiễu ablation, tốn token |
| Translator tự gọi tool nhiều vòng | Như trên |
| Rule đếm câu EN=VI trong Critic Tier 1 | Sai cho văn học |
| Surface-match check glossary/entity | Ép verbatim cửa sau (PROMPT_DESIGN §1.6) |
| Late Chunking, Matryoshka/MRL, bake-off embedding | RAG-trend, không phục vụ RQ nào |
| Graph DB riêng (Neo4j…) + GraphRAG machinery (community detection, multi-hop) | Nhu cầu dịch = 1-hop keyed lookup; SQLite đủ (§5.2) |
| Agentic RAG runtime (LLM tự chọn query, lặp tự do) | Phá tái lập + ablation; thay bằng Coverage gate tất định (§5.2) |
| Seeding dataset JSON vào runtime memory | Vi phạm Directional Lock §0 — memory phải do pre-pass tự xây |
| TTL/cold-archive ChromaDB, production multi-user | Over-engineering |
| Train/fine-tune model | Ngoài phạm vi từ đầu |

## 8. CHƯA CHỐT — hàng đợi thảo luận tiếp theo ⬜

1. ~~Base model cho Translator~~ → **đã chốt mềm ở §2.2** (gpt-5.4-mini + Gemini judge);
   chốt cứng sau pilot go/no-go.
2. ~~Embedding model~~ → **đã chốt mềm ở §4.2** (`text-embedding-3-large`).
3. ~~Cuốn sách đầu tiên~~ → **chốt mềm 2026-06-11: D2L (GVHD chỉ định), dual-track — xem §6.1.**
   Còn mở phần: (3a) kiểm kê coverage bản dịch d2l-vi theo chương + pin commit snapshot;
   (3b) ~~chọn cuốn văn học~~ → **chốt mềm: Treasure Island nguyên cuốn (Claude đề xuất,
   thay Alice của V3 D2)** — xem §6.1; còn mở: xác nhận với GVHD việc giữ track văn học
   song song; gold AI-LAB annotate 4–5 chương quanh arc Jim↔Silver (lưu ý: `gold_demo_01`
   hiện là SYNTHETIC demo, chưa phải dữ liệu thật); cuốn cross-check phụ chốt ở pha
   thí nghiệm; cập nhật motif seeds/prompt samples V3 từ Alice → Treasure Island khi chạm tới.
4. ~~Ngưỡng JIT~~ → **chốt mềm 2026-06-11**: trigger Narrative khi thỏa 1 trong 3 —
   (a) dialogue regex; (b) entity first-mention trong chương; (c) motif anchor + vector
   ≥ threshold. Benchmark vẫn uniform; calibrate trigger-rate ở pilot.
5. ~~Confidence tier B/D~~ → **CHỐT CÓ (✅)**: mọi field agent điền kèm `confidence`
   (slot sẵn); dùng cho memory-quality vs gold + ưu tiên inject khi đụng trần token.
6. **Trình GVHD baseline S-LC** để chốt cứng (gộp buổi báo cáo pilot P5).
7. ~~Code layout~~ → **CHỐT (✅, REVISED 2026-06-11 sau khi CodeX clone)**: toàn bộ code
   thesis sống trong `research/agent-based-translation/THESIS_RUNTIME_TOOL/` (bản clone
   app/tool từ AILAB_HANDOFF, đã loại .git/ailab_projects/output/test-results):
   - `app/` = donor code (extraction/validation) + tương lai run-viewer UI demo (KHÔNG core);
   - `dataset_spec/` = schema 1.5.0 + validate.py vendored;
   - **`pipeline/` = MỚI, toàn bộ runtime thesis**: `ingest/ prepass/ memory/ retrieval/
     context/ agents/ critic/ runner/ eval/ configs/ scripts/` + `tests/`;
   - `projects/` = data jobs (đổi tên từ `ailab_projects`).
   Code mới CHỈ viết vào `pipeline/`, không trộn vào `app/` (giữ rõ ranh giới vendored
   vs đóng góp cho Chương 3). Config-driven; migration additive như cũ.
   **Checklist vệ sinh trước khi sửa code:** (1) commit baseline clone; (2) PROVENANCE
   vào README; (3) đổi env `AILAB_PROJECTS_ROOT` → `THESIS_TOOL_PROJECTS_ROOT`
   (config.py + test_api_smoke.py); (4) đánh dấu annotation_flow/review_state =
   không-phát-triển (human gate); (5) docs AI-LAB cũ trong clone gắn header "chỉ tham khảo".

### 8.1. Tái sử dụng code từ AILAB_HANDOFF ✅ (chốt 2026-06-11)

**Quy tắc:**
1. **Chiều: thesis ← handoff OK; handoff ← thesis CẤM** (handoff = deliverable lab, scope khóa).
2. **COPY/vendor vào `thesis/` + ghi provenance, KHÔNG import xuyên ranh giới** — handoff
   tiến hóa độc lập, thesis cần ổn định để tái lập.
3. **Code reuse được, DATA gold thì không** (eval-only). Riêng `canonical/document.json`
   (cấu trúc block văn bản gốc do tool sinh) dùng được cho runtime — là source text,
   không phải kết quả người làm.

**Map reuse:** `extraction.py`+`structure_normalizer.py` → `ingest/` (EPUB pipeline đã
chạy thật); `dataset_spec/schema/*.json` → validate World Builder output (nới provenance
người); `validate.py` span/relations checks → `prepass/span_resolver` + validation;
`dataset_io/workspace` pattern → `runner/`; skill `dataset-annotation-drafter` → tham
khảo prompt World Builder (đổi mission: draft-for-human → autonomous + confidence).
**KHÔNG reuse:** web UI/frontend, `annotation_flow`/`review_state` (cổng người duyệt),
`human_feedback` paths.

**Bonus phát hiện:** `call_wild_epub` + `canterville_ghost_epub` đã ingest sẵn → ứng
viên cross-check book (§6.1) chi phí ingest = 0; Treasure Island chỉ cần chạy qua
pipeline EPUB có sẵn. **Hệ quả:** P1 co lại = vendor extraction + adapter D2L markdown
+ glue document.json→SQLite blocks + test idempotency.

### 8.2. Quy trình làm việc & truy vết ✅ (chốt 2026-06-11)

**Ba nhà của thông tin — không trộn:**
| Loại | Nhà | Trả lời |
|---|---|---|
| Quyết định + lý do | LOCK file này + §10 changelog | "TẠI SAO?" |
| Công việc + tiến độ | `THESIS_RUNTIME_TOOL/tasks/` + `LEDGER.md` | "Việc gì, ai, kết quả?" |
| Lịch sử mã | Git (commit gắn task-id, tag phase) | "KHI NÀO, diff gì?" |

**Vòng đời task** (1 việc = 1 file `TASK_<Px>_<nn>_<slug>.md`, theo `TASK_TEMPLATE.md`):
Claude viết spec §1–§4 (acceptance = LỆNH chạy được) → user đưa CodeX imple, điền §5
(notes + test output) → Claude review điền §6 verdict → cập nhật 1 dòng LEDGER.
Gotcha kỹ thuật → §5 task; thành quyết định dài hạn → LOCK changelog.

**Git (REVISED 2026-06-11, user quyết — đảo quyết định "không nested repo" trước đó):**
`research/agent-based-translation/` = **repo git ĐỘC LẬP** (nested), nhánh chính `main`.
Lý do: lịch sử odl-pdf-demo là PDF app (lớp 3 adapter) — thesis cần repo sạch, lịch sử
độc lập để trình hội đồng. Repo ngoài đã gỡ folder khỏi index + ignore (commit 6c53692).
Baseline repo mới: `ba926c5` (vendor) → `59255cb` (docs) — tương đương 5238d2f/7d523ef
của repo cũ. Convention giữ nguyên: commit `P0-01: …` / `docs: …` / `vendor: …`
(1 task = 1 commit chính); tag mốc `P0-done`…`P5-pilot`. `.gitignore` loại AI-LAB track
(AILAB_HANDOFF/ailab/AILAB_SOURCES_RAW) + runtime data (projects/, *.sqlite3, data/jobs).
Lưu ý: các nhánh PDF cũ của repo ngoài vẫn track file doc ở đường dẫn cũ — khi quay lại
nhánh đó, cherry-pick commit 6c53692 để khỏi lòi file lịch sử vào nested repo.

**Cold-start protocol cho agent mới:** đọc (1) LOCK → (2) LEDGER → (3) đúng TASK liên
quan. Hết. Không lục V3/lịch sử chat để truy vết công việc.

## 9. Lộ trình thực thi P0–P6 ✅ (chốt 2026-06-11 — hướng đi chính thức)

> Thiết kế đã bão hòa: mọi mục còn mở đều cần SỐ PILOT hoặc GVHD, không cần bàn thêm
> trên giấy. Đường găng = P0→P5 ("có số đưa GVHD" nhanh nhất); thứ đắt/rủi ro đẩy sau
> pilot để nếu phải đổi hướng thì mới tốn 2 chương, không phải cả hệ.

| Phase | Việc | Cổng ra (exit gate) |
|---|---|---|
| **P0 Scaffold** | `thesis/` skeleton + migration 003 (5 bảng + cột config + schema_version 3) + LLM client (pin model/seed/reasoning_effort, replay cache) + YAML config | Migration sạch trên DB mới VÀ DB prototype cũ không vỡ (test) |
| **P1 Ingest** | Adapter d2l-vi (gồm 3a: kiểm kê coverage + pin commit) + adapter Gutenberg (Treasure Island); rule code/math placeholder | `block_id` idempotent (chạy lại ra y hệt) |
| **P2 Pre-pass** | World Builder tuần tự + Span Resolver + Consolidation + FREEZE | Registry T1–T4 trên 2 chương TI; JSON fail <5% (go/no-go #1) |
| **P3 S0 end-to-end** | State machine tối thiểu + ghi translation_runs/memory_packs | **2 chương dịch trọn S0 = mốc "dịch được" của GVHD** |
| **P4 S3** | Hard constraints + rolling + summary (S1/S2 tự rơi ra) → vector + Brief + Critic 2 tier + Repair | 2 chương chạy S3 uniform; token_breakdown khớp budget §5 |
| **P5 PILOT** ⭐ | TAR/ECS internal + COMET-Kiwi + judge GEMBA-DA + memorization test; calibrate threshold/top-k; go/no-go model (§2.2) | **Báo cáo 2 trang cho GVHD: số S0-vs-S3 + 2 câu hỏi (dual-track, S-LC)** |
| **P6 Scale** | Full 2 cuốn; ladder đủ + S-LC + S3a/b/d; D6 queries; MQM/pairwise; backtranslation; human E5 | Số liệu Chương 4 |

P3 = mốc tâm lý quan trọng nhất: từ đó luôn có pipeline chạy được để demo.

## 10. Changelog

- **2026-06-11** — Tạo file. Chốt: pipeline 2 phase + code coordinator + World Builder
  consolidation (§2); SQLite reuse + delta 5 bảng/1 cột + freeze rule (§3); ChromaDB 3
  collections (§4); inject budget + anchor-based selection (§5); khung thí nghiệm + đề
  xuất S-LC + pilot sớm (§6); danh sách loại scope (§7). Mở: §8 items 1–7.
- **2026-06-11 (b)** — Chốt mềm model stack (§2.2): user có OpenAI free quota (250k/ngày
  dòng lớn, 2.5M/ngày mini) → translator/pipeline = `gpt-5.4-mini` pinned; judge +
  backtranslate = Gemini (cross-provider, free tier); embedding = `text-embedding-3-large`
  (§4.2). Lý do đổi từ đề xuất Gemini Flash ban đầu: quota free + seed/system_fingerprint
  + structured outputs; S-LC vừa 400k context với sách ngắn. §8 items 1–2 resolved.
- **2026-06-11 (c)** — Chốt mềm dataset 2 track (§6.1): GVHD chỉ định D2L → track kỹ
  thuật chạy end-to-end đầu tiên (reference d2l-vn + Bảng thuật ngữ = gold T1); track
  văn học GIỮ cho RQ5/narrative (C2/C3). Rủi ro D2L: leakage high (phòng thủ = ablation
  delta + memorization test), version pinning theo repo d2l-vi, kiểm kê coverage, rule
  code/math placeholder. Mở thêm: §8 items 3a (kiểm kê + pin commit), 3b (chọn cuốn văn
  học + xác nhận GVHD dual-track).
- **2026-06-11 (d)** — Chốt mềm cuốn văn học lần 1: Alice in Wonderland (giữ V3 D2).
  Phát hiện: `gold_demo_01` là synthetic demo → việc thật đầu tiên của AI-LAB track =
  annotate cuốn văn học được chọn.
- **2026-06-11 (e)** — **REVISE (d): cuốn văn học = Treasure Island** (user yêu cầu
  Claude đề xuất độc lập, không ràng buộc theo gold/V3). Lý do đổi: Alice episodic
  (nhân vật 1–2 chương, memory ít đất diễn) + puns nhiễu metric; Treasure Island có
  arc Jim↔Silver đổi pha (showcase entity_relations 3 tầng + xưng hô VI động), cast
  cố định 34 chương, alias + bẫy "Captain Flint"×2, POV switch ch16–18, T1 hàng hải.
  ~67k từ vẫn chạy nguyên cuốn trong quota. Gold AI-LAB: 4–5 chương quanh arc
  (ch8/ch11/ch14–15/ch28). Reference = manual subset tự tạo.
- **2026-06-11 (f)** — Chốt agent architecture (§2.1): 2 loại × 2 tầng, đúng 4 LLM agents
  (World Builder / Narrative / Translator / Critic T2; Repair & Consolidation & Judge
  không phải agent riêng); nguyên tắc "LLM không chạm DB" + bảng quyền hạn; state machine
  per block (1–4 call, điển hình ~2) + failure policy (re-ask 1 lần, fail-open đi tiếp,
  checkpoint resume); tool = Deterministic Context Feeding qua Coordinator, KHÔNG
  function-calling/ReAct ở runtime (agentic retrieval = V2 future work); World Builder
  scan TUẦN TỰ theo chương với registry-so-far nén + 1 call Consolidation cuối.
- **2026-06-11 (g)** — Chốt khung chấm benchmark (§6.2): profile 4 trục (từ/nghĩa/mạch/
  giọng), không điểm tổng; định nghĩa TAR/ECS khóa theo nguyên tắc consistency≠verbatim;
  đề xuất ACS optional (đo xưng hô theo pha); judge 2 chế độ MQM-lite (trọng số 1/5/10/25)
  + pairwise A/B đảo vị trí 2 lần + calibrate Spearman ρ vs human; 2 track chấm khác nhau
  (D2L ref-based có nghĩa, văn học ref-free + preference); backtranslation = diagnostic
  per-block + đo tương quan với judge; paired bootstrap + CI; lịch chấm pilot→full→human.
- **2026-06-11 (h)** — Chốt cơ chế Cache & Compact (§5.1, GVHD nhấn mạnh): prompt 3 zone
  (tĩnh cả run / bán tĩnh theo chương / động theo block); REVISE budget §5 — zone tĩnh
  cố tình làm giàu ~1.100–1.300 tok để vượt ngưỡng cache 1024 của OpenAI (giàu-cached rẻ
  hơn gọn-uncached ~5×); kỷ luật byte-identical + chạy tuần tự theo config; replay cache
  theo (model, prompt_hash, temperature, seed) + embedding cache theo content-hash;
  reasoning_effort minimal/low cho Translator (gpt-5 tính tiền reasoning tokens); luận
  điểm O(1) token/block vs O(n) của S-LC; compact 4 quy tắc (template cố định, rolling
  window chỉ VI, brief thay raw, không chat history).
- **2026-06-11 (i)** — Chốt retrieval (§5.2): định vị "GraphRAG-shaped offline,
  keyed-lookup online" (đa số context = tra cứu có khóa nhờ pre-pass đánh chỉ mục theo
  block_id; vector chỉ cho soft); 4 lưới chống thiếu + 5 van chống thừa; threshold trước
  top-k; đo retrieval riêng bằng D6 + RetrievalLog; loại thêm khỏi scope: graph DB/
  GraphRAG machinery, agentic RAG runtime; **vô hiệu RETRIEVAL_ARCHITECTURE §B seeding**
  (mâu thuẫn Directional Lock §0) và §D global core 400–600 (thay bằng Zone 1 §5.1).
- **2026-06-11 (j)** — Đóng nốt retrieval (§5.2 + §4.1): embedding space **thuần EN**
  (TM embed vế EN làm khóa, VI payload — loại cross-lingual bằng thiết kế; đóng
  RETRIEVAL_ARCHITECTURE §N.1); granularity per collection; tiêu chí "retrieval đủ"
  3 cửa (hard ≥99%, low_context <5%, D6 Recall@5 pilot); còn nợ: calibrate
  threshold/top-k ở pilot, tạo D6 queries, đo giả định embedding-văn-học.
- **2026-06-11 (k)** — Đóng 3 mục thiết kế cuối + chốt hướng đi: JIT trigger = OR 3 tín
  hiệu (§8.4, chốt mềm); confidence tier B/D = CÓ (§8.5 ✅); code layout = monorepo
  package `thesis/` config-driven (§8.7 ✅); thêm **§9 Lộ trình thực thi P0–P6** —
  đường găng P0→P5 đến "có số đưa GVHD", thiết kế tuyên bố BÃO HÒA: mục còn mở chỉ chốt
  được bằng số pilot hoặc GVHD, chuyển trạng thái từ thảo luận sang thực thi.
- **2026-06-11 (l)** — Chốt tái sử dụng AILAB_HANDOFF (§8.1): chiều thesis←handoff,
  vendor-không-import, code-không-data; map reuse (extraction/normalizer → ingest,
  schema+validate → validation, drafter skill → tham khảo World Builder prompt); loại
  web UI + annotation_flow; phát hiện call_wild + canterville đã ingest = ứng viên
  cross-check miễn phí; P1 co lại đáng kể.
- **2026-06-11 (m)** — CodeX thực thi vendor: clone app/tool → `THESIS_RUNTIME_TOOL/`
  (83/83 app, 20/20 dataset_spec, 14/14 skills, 8/8 tasks; loại .git/ailab_projects/
  output/test-results). Hợp lệ theo §8.1. **REVISE §8.7**: code thesis sống trong
  THESIS_RUNTIME_TOOL, runtime mới viết vào `pipeline/` (không trộn `app/`); checklist
  vệ sinh 5 bước trước khi sửa code (baseline commit, provenance, env rename
  THESIS_TOOL_*, đánh dấu human-gate không-phát-triển, header docs cũ). `app/` được
  nâng vai trò tiềm năng: run-viewer UI demo hội đồng (vẫn không phải core).
- **2026-06-11 (n)** — Chốt quy trình làm việc & truy vết (§8.2): 3 nhà thông tin
  (LOCK = quyết định; tasks/LEDGER = công việc; git = lịch sử mã); vòng đời task
  spec(Claude)→imple(CodeX)→review(Claude) gói trong 1 file; git: nhánh `thesis/main`,
  commit theo task-id, tag mốc phase; cold-start = LOCK → LEDGER → TASK. Đã tạo
  `THESIS_RUNTIME_TOOL/tasks/LEDGER.md` + `TASK_TEMPLATE.md`.
- **2026-06-12 (r)** — Phân tích human-vs-oracle ch1: bản người
  (`AILAB_SOURCES_RAW/treasure_island/translatebyhuman_Chapter1.txt`) là **LƯỢC DỊCH/
  PHÓNG TÁC** (cắt đoạn mở đầu kinh điển, nén thoại ~1/2, sáng tác thêm, phiên âm
  Bin-bâu/Ly-vơ-xây) — KHÔNG dùng làm reference per-block (lệch align + thiếu nội dung),
  KHÔNG vào pipeline; dùng làm (a) bài test hiệu chuẩn judge (judge phải nhận ra bản
  người tự nhiên hơn oracle thì mới đáng tin), (b) tư liệu error taxonomy. Gap máy-người
  tách 3 tầng: calque/chồng tính từ/literal simile (SỬA được = việc của Critic T2 style
  + Repair — bằng chứng thực nghiệm tầng này cần tồn tại); độ nén (DIAL được — thêm
  "giấy phép nén có kiểm soát" vào style policy Zone 1, đánh đổi với trục đúng-nghĩa);
  tái sáng tạo văn hóa như câu hát (giới hạn LLM → Chương 5 limitation). Tái khẳng định
  mục tiêu: hơn S0 đo được + leo quãng S0→oracle chi phí thấp, KHÔNG phải "bằng người".
- **2026-06-12 (q)** — Ghi nhận **oracle reference run** (§6.3): CodeX GPT-5.5 annotate
  full 40ch Treasure Island (chất lượng tốt: glossary đúng kỷ luật §1.6, 69/69 relations
  có pha, summary thực chất) + đang dịch preview 40ch. Vai trò: mỏ neo trên + rehearsal
  + draft cho gold + đối thủ pairwise; CẤM làm baseline ladder/reference/memory thesis.
  Prompt dịch của user được review: thêm log used-context per block, siết "tự sửa" về
  preview-only, ghi cost per chapter, khai non-reproducibility trong final report.
- **2026-06-11 (p)** — **Tách repo độc lập** (user quyết, REVISE §8.2 git): git init tại
  `agent-based-translation/` (nhánh `main`, baseline ba926c5+59255cb); repo ngoài
  odl-pdf-demo gỡ folder + ignore (6c53692). Lý do: lịch sử PDF app không liên quan
  thesis; repo sạch để trình hội đồng. Nhánh `thesis/main` repo ngoài thành mồ côi
  (giữ làm archive). Việc còn lại: tạo remote GitHub private + push.
- **2026-06-11 (o)** — Tái cấu trúc thư mục (3 vùng): LOCK + README giữ root;
  `design/` = 6 doc đang hiệu lực (V3, RUN_EVAL_SCHEMA, PROMPT_DESIGN,
  RETRIEVAL_ARCHITECTURE, SCHEMA_AGENT_FILL_POLICY, DATASET_DESIGN); `reference/` =
  transcript/tham khảo + papers; `tasks/` root cũ → `archive/legacy-tasks/`
  (entity_relations đã hoàn thành trong schema 1.5.0). Track AI-LAB (AILAB_HANDOFF,
  ailab, AILAB_SOURCES_RAW) không đụng. Làm trước baseline commit nên không mất
  lịch sử git (chỉ 4 file tracked, đã dùng git mv).
