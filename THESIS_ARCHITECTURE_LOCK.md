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
  → PHASE 2 RUNTIME (per WINDOW — cửa sổ 1–N block liền kề, xem §5 đơn vị dịch):
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
| Translator tool-use | **KHÔNG tự gọi tool**; 1 call/**window** (1–N block, §5) + META `uncertain_spans`; output JSON keyed block_id | Tái lập, chi phí; Coverage Checker chặn trước, Critic soi sau |
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

**State machine per WINDOW** (Coordinator; config S0..S3 bật/tắt từng bước; window =
đơn vị dịch §5, lưu trữ + metric vẫn per block):

```
PLAN→RETRIEVE→BUDGET→CHECK (code, 0 token; thiếu anchor → re-retrieve 1 lần → flag low_context)
→ BRIEF (S3; benchmark=uniform)        call #1
→ TRANSLATE (context pack đóng băng)   call #2
→ CRITIC-1 (code rules; pass sạch → PERSIST)
→ CRITIC-2 (nếu Tier1 không sạch HOẶC có uncertain_spans)  call #3
→ REPAIR (critical/major, max 1)       call #4
→ PERSIST: translation_runs + memory_packs(token_breakdown) + qa_issues; pass → embed TM
```
→ 1–4 call/window, điển hình ~2 (window ~4–6 block → call/block giảm ~4–6×).

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

**ĐƠN VỊ DỊCH = WINDOW ✅ (chốt 2026-06-12, thay "1 call = 1 block"):**
- Window = **chuỗi block LIỀN KỀ cùng chương**, đóng gói theo **ngân sách token nguồn**
  (target 🔶 ~1.000–1.500 tok source/window, ~4–6 block — số chính xác pilot quyết),
  tối thiểu 1 block; block đơn vượt budget → window 1 block oversize, **KHÔNG cắt block**
  (cắt phá trục block_id/metrics; block văn xuôi hiếm khi vượt).
- Quy tắc biên window (code tất định): KHÔNG vượt biên chương; KHÔNG cắt giữa chuỗi
  dialogue-block liên tiếp nếu chuỗi vừa budget (mạch thoại dịch trong 1 call); BẮT BUỘC
  cắt tại block trigger đổi pha `entity_relations` (mỗi window chỉ 1 state active/entity).
- Output: Structured Outputs JSON `{block_id: target_text}` — đủ mọi block_id của window,
  thiếu/thừa → re-ask 1 lần (failure policy hiện có). **Lưu trữ + metric + Critic vẫn
  per block** (translation_runs 1 hàng/block, thêm cột `window_id` khi P3); TAR/ECS
  không đổi định nghĩa.
- Retrieval 1 lần/window = UNION anchor của các block thành viên (sub-linear — thoại
  lặp 2 nhân vật thì union gần như không phình); hard constraints render bảng nén
  keyed theo block_id; rolling window = đuôi VI của window trước.
- Lý do chốt: (a) Zone 1+2 (~1,3–1,5k tok) đang bị lặp lại MỖI block → window trả 1 lần
  cho 4–6 block, call count giảm ~4–6×; (b) block thoại ngắn lặp qua lại = mạch hội
  thoại bị băm — dịch cả lượt thoại trong 1 call là giả thuyết chất lượng đo được ở
  pilot; (c) thu hẹp confound khi so với oracle (oracle dịch nguyên chương trong context).
- Áp dụng ĐỒNG NHẤT cho mọi nấc S0→S3 (đổi đơn vị dịch mà chỉ áp 1 nấc = ablation bẩn).

**Quy tắc chọn context (code tất định, 0 token):** chỉ nhét thứ match **anchor** trong
window — glossary có occurrence trong block thành viên, entity card có mention, discourse
chỉ khi có thoại, brief chỉ ở S3. KHÔNG BAO GIỜ dump cả registry.

| Zone (thứ tự trong prompt) | Budget/window | Tính chất |
|---|---|---|
| **ZONE 1 — TĨNH CẢ RUN**: system prompt + output contract + style policy + book synopsis (~150) + top-10 main character cards + hot glossary top-20 one-liner | **~1.100–1.300 tok** (cố tình ≥ ngưỡng cache 1024 — xem §5.1) | Cache ~10% giá |
| **ZONE 2 — BÁN TĨNH THEO CHƯƠNG**: chapter summary + motif notes | ~200 tok | Cache hit trong chương |
| **ZONE 3 — ĐỘNG**: hard constraints anchor-based union (≤500) + rolling đuôi VI window trước (~300) + Brief S3 (150–300) + source window (~1.000–1.500) | ~1.500–2.600 tok | Trả full giá |
| **Tổng input/window** | **~2.800–4.100 (S3)** cho 4–6 block — tức ~600–900/block, RẺ hơn ~2.000–2.900/block của thiết kế 1-call-1-block cũ | |

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
- **2026-06-16 (oo)** — **Live-view của App = THANG LEO A→D→B/C, KHÔNG nhảy thẳng per-window commit (chốt 3 bên: user muốn "xem từng block hiện ra live"; Claude nêu gốc kỹ thuật + khuyên A trước; CodeX sắc hóa hybrid-abort + đề D sidecar; cả 3 hội tụ A→D→B/C).** **(1) GỐC KỸ THUẬT (Claude+CodeX tự kiểm code):** `pipeline/translate/runner.py:238` gọi `db.commit()` MỘT lần ở cuối vòng dịch (`_persist_run`/`_persist_pack` ghi trong loop, commit cuối). SQLite isolation: connection read-only của cockpit CHỈ thấy dữ liệu đã-commit (WAL reader cũng chỉ thấy snapshot committed) → KHÔNG thể "nhìn trộm" row giữa run. ⇒ live per-block bằng cách tail DB là BẤT KHẢ THI nếu không đổi cadence commit. `run_id = tr_{config}_{block_id}` + `INSERT OR REPLACE`, KHÓA không chứa attempt/experiment (`experiment_id` chỉ là cột) ⇒ chạy lại cùng config ĐÈ âm thầm = **lỗ provenance CÓ SẴN** (không giữ được 2 attempt song song), độc lập với live-view. **(2) 4 HƯỚNG + đánh giá:** **A. Tail-LOG** — UI tail stdout/stderr, xong run refresh A/B/D read-model; KHÔNG đổi engine, GIỮ all-or-nothing; **đã làm trong APP_C01 (DONE)**, khớp (nn).3. **B. Commit mỗi window** — block hiện trong DB/viewer từng window; **KHÔNG mặc định** (phá all-or-nothing). **C. Commit mỗi window sau cờ** — bật khi demo/debug; chỉ làm nếu có run-manifest rất chặt. **D. Structured sidecar event log (TỐT NHẤT sau A)** — engine append JSONL event/window ra `run_events/<attempt_id>.jsonl` (window_started/prompt_built/request_sent/response_received/json_parsed/window_preview_available/persist_buffered/run_committed/run_failed); UI tail FILE đó; **DB vẫn commit cuối**. Triết lý: D **THÊM quan sát mà KHÔNG đổi tính toán** → deterministic engine + scorer SẠCH; crash thì event log chỉ là artifact debug, scorer KHÔNG BAO GIỜ chấm nhầm. Đúng observe⊥compute hơn B/C. **(3) HYBRID-ABORT = giá của B/C (CodeX sắc hóa, Claude nhận mình nói nhẹ):** nếu per-window commit và run MỚI abort giữa chừng → block đã-chạy = output mới, block chưa-chạy = output CŨ cùng `config` → scorer/viewer lọc theo `config` đọc **bản lai cũ-mới**. Chính commit-cuối hiện tại đang BẢO VỆ khỏi điều này (abort=rollback=sạch); per-window commit GỠ lớp bảo vệ đó. Claude từng nói "`INSERT OR REPLACE` làm B an toàn hơn vẻ ngoài" — chỉ đúng cho re-run TRỌN VẸN, SAI cho re-run abort. Muốn B/C an toàn: bảng **`run_attempts` RIÊNG** (`attempt_id`, `status=running|complete|aborted`, `prompt_version`, `model`, `seed`, `started_at`, `ended_at`) + scorer/read-model CHỈ chấm `status=complete`; status thuộc "lần-chạy", KHÔNG nhét vào từng row `translation_runs`. **(4) 3 GUARD cho D (Claude thêm):** (i) sidecar preview phải dán nhãn **"uncommitted"**, CẤM rò vào read-model/report — provenance-guard phiên bản D (như gold không map vào `glossary_entries`, preview không map vào translations chính); (ii) `attempt_id` giới thiệu RỦI-RO-THẤP ở tên file sidecar TRƯỚC (chưa đổi schema DB), chỉ nâng vào KHÓA `run_id` + `run_attempts` khi thật cần B/C; (iii) sidecar **live-only/ephemeral** — sau commit cockpit đọc DB là NGUỒN-SỰ-THẬT-DUY-NHẤT (tránh 2 bản ghi lệch); D cũng là **kênh cost-gate live** (token/cost/window → phục vụ quality-per-token, không chỉ mỹ phẩm). **(5) KHÔNG token-streaming (3 bên đồng ý):** output là JSON theo window/block; stream từng token khó parse, nhiễu UI, ít giá trị thesis. Cái cần live = **lifecycle từng request** (prompt/context/cache/token/cost/parse/block-update), KHÔNG phải từng chữ model sinh. **(6) THỨ TỰ:** A (APP_C01, DONE) → **D (`RUN-EVENT-01` / có thể gọi `APP_C02`, BACKLOG, sau khi C01 ổn)** → B/C CHỈ sau khi có `run_attempts` + guard chống partial/mixed-run. Nối (nn).3 (C01 tail-log) + (ll) cost-as-GATE; bảo vệ deterministic engine (Directional-Lock + replay-cache).
- **2026-06-15 (nn)** — **App khóa luận = RESEARCH COCKPIT (quan sát/truy vết/điều khiển/báo cáo), TÁCH RỜI engine pipeline headless; seam = SQLite pipeline → UI read-model; 4 màn `APP_A01→B01→D01→C01`; provenance tách CẤU TRÚC; quarantine-KHÔNG-xóa AILAB (chốt 3 bên: user nêu tầm nhìn cockpit + siết scope/tên; CodeX thêm quarantine + 2-read-model + shape provenance + đặt tên; Claude nguyên tắc observe/compute + provenance-cấp-query + drift-view + metric-traceable).** **(1) Ranh giới SỐNG CÒN observe ⊥ compute:** App CHỈ (a) trigger run + (b) đọc DB/logs/cache để hiển thị; pipeline giữ headless/batch/DETERMINISTIC (seed + replay-cache + memory frozen). CẤM UI giữ state thật / sửa memory trực tiếp / tự tính metric riêng → mất tái-lập = yếu luận văn. Lợi: app crash KHÔNG kéo pipeline; replay-cache → app dựng lại y hệt cho hội đồng 0-API. **(2) Stack + seam (Claude+CodeX tự kiểm code):** frontend React-UMD + Babel-standalone trong browser (`app/prototype/*.jsx`, 0 build → fork dễ); backend Flask blueprint + services đọc workspace `projects/*/{canonical,working}/*.jsonl`; pipeline ghi `data/jobs/*/memory.sqlite3` (`glossary_entries`/`entities`/`entity_relations`/`memory_packs`/`translation_runs`/`evaluation_runs`/`reference_eval_only`/`llm_call_cache`). → việc thật KHÔNG phải UI mà là **READ-MODEL adapter SQLite→shape UI**; app đã có sẵn móc `/translation-preview/runs/agent-output`. **(3) 4 màn = 4 task, thứ tự A→B→D→C (bằng chứng trước, live sau):** `APP_A01` Dataset read-model + viewer (document/blocks/glossary/entities/relations/translations + metadata chọn run: experiment_id/config/stage/prompt_version — KHÔNG prompt/cache/cost chi tiết); `APP_B01` Observability cockpit (**Prompt/Context Inspector BẮT BUỘC** + API calls + cache + token + cost); `APP_D01` Score/report + **Consistency/Drift hạng-nhất**; `APP_C01` Run control/live-stream CUỐI (chỉ trigger script đã freeze + tail log; UI KHÔNG tự tính). **2+ read-model RIÊNG endpoint:** `DatasetReadModel` ⊥ `ObservabilityReadModel` — CẤM nhét cost/cache vào schema dataset 1.5.0. **(4) Provenance = guard CẤP-QUERY, không chỉ badge (siết Directional-Lock):** read-model trả shape TÁCH cấu trúc, CẤM trộn: `{runtime_memory:{glossary_entries,entities,entity_relations}, eval_only:{gold_glossary,references}, translations:{S0,S1,...}}`. UI muốn hiện gold PHẢI gọi nhánh `eval_only` — gold KHÔNG BAO GIỜ map thành `glossary_entries`. Test được ở tầng adapter (cùng triết lý guard `context_builder chỉ đọc glossary_entries`). **(5) QUARANTINE, KHÔNG xóa (CodeX sửa Claude — Claude nhận: "gỡ" là quá tay):** AILAB gold-authoring (Annotation drafter, manual reference authoring, package/freeze gold, UI Structure normalizer tương tác) ẩn sau feature-flag / route-group `ailab_legacy`, dọn sau — xóa mạnh dễ vỡ viewer dùng chung + mất undo + mất git history. Đổi hướng: Translation Preview→**Run Preview** (source + S0/S1/S2/S3 + oracle eval-only); review_state→**artifact status** (agent-built / eval-only / oracle / human-override); History→**run provenance** (model/seed/prompt_version/cache_key/usage). **(6) Consistency/Drift (`APP_D01`) = trục D NHÌN-ĐƯỢC (Claude+user):** không chỉ `D=0.70` mà click ra term/entity nào trôi, block/chương nào, S0 ra biến thể gì, S1 khóa thế nào, drift thuộc glossary/entity-name/xưng-hô; `A01` phải chuẩn bị dữ liệu sạch cho `D01`. **(7) Metric TRACEABLE (Claude+CodeX):** mỗi headline mang provenance — metric name/version, scorer command/module version, experiment_id/config, run_id/list `translation_run_id`, source scope, snapshot/report path, cache/call ids (nếu judge), calibrated? (judge). Deterministic B/D/TAR → trace `translation_runs` + scorer version; judge → trace `judge_call_cache`. Khớp luật "headline reviewer tái tính từ scorer". **(8) Human-edit = lane RIÊNG có log, LOẠI khỏi metric agent-only; run của agent BẤT BIẾN.** **Guardrail bắt buộc:** KHÔNG ghi frozen memory chính; mọi run từ UI tạo `run_id`+config+model+seed+prompt_version; prompt dài preview-được-TRƯỚC-khi-chạy; KHÔNG auth/multi-user/SaaS-polish lúc này; KHÔNG live-stream trước khi viewer+report chạy ổn. **Áp dụng: `TASK_APP_A01`** (read-only DatasetReadModel + viewer + quarantine + provenance; **0 API / 0 pipeline / 0 engine change**); roadmap B01/D01/C01. Nối (ll) prompt-artifact-review, (kk) trục D xuyên-domain, (mm) S1⊥S3 + recall/precision; bảo vệ Directional-Lock (gold eval-only KHÔNG inject).
- **2026-06-15 (mm)** — **Literary Builder hậu-HYG-01: đơn vị = CHƯƠNG (bất đối xứng CÓ NGUYÊN TẮC vs D2L window) + ranh giới S1-exact ⊥ S3-semantic + recall-at-build/precision-at-inject (BỎ cap glossary) + 4 process-guard (chốt 3 bên: Claude đề 4 trục, CodeX hội tụ + thêm threshold/density-audit/relation-label/4-guard; render-fidelity CodeX bắt, Claude nhận).** **(1) Đơn vị Builder văn học = CHƯƠNG (mặc định), KHÔNG window-nhỏ kiểu D2L:** D2L window an toàn vì term exact consolidate XÁC ĐỊNH (cùng surface=merge, fragment vô hại); entity-coreference/alias/address-state/motif/summary của văn học PHỤ-THUỘC-MẠCH-CHƯƠNG → window xé continuity + gộp entity mờ dễ sai. Nhưng KHÔNG tuyệt-đối-hóa "luôn chương": preflight theo ngưỡng token — `OK ≤8k` / `WARN 8–12k` (chạy nhưng phải review) / `SPLIT >12k` / `ABORT >20k` (nếu chưa có split strategy). Khi SPLIT: large windows theo cảnh/đoạn (4–6k source tok, overlap 1–2 block) + CARRY-IN-PROGRESS (entities đã phát hiện trong CHÍNH chương + alias mới + active relations gần nhất + narrator card + chunk summary ngắn) + consolidate cuối chương; bước chapter-level consolidation từ chunk-summaries = TASK RIÊNG. TI chương ngắn (~4.5k) → KHÔNG bao giờ trip → HYG-02 chỉ dựng DETECTOR + status + ABORT, KHÔNG build executor split (YAGNI). **(2) Ranh giới S1-exact ⊥ S3-semantic (giữ ablation SẠCH):** S1 Builder pack = continuity EXACT-SURFACE; motif/ẩn-ý tái xuất NGỮ-NGHĨA = S3 retrieval (Chroma motif collection đã có ở P4-01). CẤM fuzzy-inject vào S1 — nếu nhồi, S1 hết là hard-memory-exact, lẫn sang S3 → bẩn ranh giới ablation. Khóa luận KHÔNG claim "Builder hiểu motif" bằng exact matcher; ví dụ `seafaring man with one leg` (ch02) vs `seafaring men, with one leg or two` (ch03) = case S3, KHÔNG phải S1 (câu chuyện đẹp: S1 bắt mặt-chữ, S3 bắt vọng-nghĩa). Tùy chọn `near_miss_candidates` REPORT-ONLY (KHÔNG inject) để soi trước S1-miss→S3-catch — để task riêng. **(3) recall-at-build / precision-at-inject → BỎ cap "Aim for 5-20 glossary terms" (di sản):** cap cấp-chương trên call cả-chương ÉP kìm recall ở bước build; hậu HYG-01 injection đã lọc relevance → registry to KHÔNG còn phình prompt → trích ĐỦ ở build, lọc ở inject. KHÔNG đặt cap cứng MỚI (vd "tối đa 40" tái tạo lỗi rớt-term của D2L). Guard thay thế: giữ termhood bar + negative examples (council/chart/bearing/parlor/basin/breakfast/stroke; KHÔNG glossary-hóa người/đại từ/đồ thường ngày) + DENSITY AUDIT sau build (glossary/chapter; glossary trên 1k source-token; hapax count; category distribution; 20 mục mới làm ví dụ) + cảnh báo bất thường (density ≥2–3× chương trước → DỪNG review, KHÔNG auto-chạy Translator; chỉ fire từ chương ≥2 vì cần baseline). Văn học KHÔNG có gold soi termhood như D2L → density-audit là LƯỚI AN TOÀN thay gold. **(4) Relation render compact + nhãn xã-hội ngắn:** giữ `A<->B: address_a→b / address_b→a (state_label)`, THÊM nhãn quan hệ ngắn (`[lodger/inn-boy]`, `[father/son]`) vì cùng cặp xưng hô khác SẮC THÁI theo vai; `notes` KHÔNG mặc định (dài+nhiễu), chỉ thêm khi cờ `address_shift`/`conflict`/`revealed_identity`. **(5) 4 PROCESS-GUARD bắt buộc (CodeX đề, nối ll):** (a) bump `prompt_version` mọi lần đổi byte (`literary_builder_context_v2`→`v3`); (b) render-CHRONOLOGY guard thành TEST — preview Builder chương N lấy registry từ ARTIFACT chương <N, CẤM DB frozen đã merge (chống prompt "đẹp giả vì thấy tương lai" — gốc: sample HYG-01 cũ render từ DB merged ch02+ch03 nên rò Black Dog/rum/lancet vào context ch03); (c) Builder PREFLIGHT toàn bộ chương định chạy, KHÔNG chỉ 1 sample — bảng `chapter_id | source_tokens | context_pack_tokens | prompt_tokens | included/excluded/dropped | status`; (d) cache-friendliness — system/schema đứng ĐẦU + byte-identical xuyên chương, context pack SORT cố định, KHÔNG timestamp/random (điều kiện để cache provider có cơ hội hoạt động). **(6) Cost-quality GATE đặt ở TASK RE-BASELINE (KHÔNG ở HYG-02 vì HYG-02 không chạy LLM):** trước re-baseline TI phải có bảng "S1 thêm bao nhiêu token vs S0" + "memory pack chiếm % prompt"; S1 đắt hơn S0 nhiều mà chưa có lý do chất lượng → DỪNG (hiện thực hóa (ll).2 cost-as-GATE). **Áp dụng: TASK HYG-02** (IN: bỏ cap→density audit, nhãn relation, bump v3, render-chronology guard test, full-set preflight + threshold/abort, cache-friendliness assert, fold render-đúng-thời-điểm; OUT: split executor + chapter-level consolidation + near_miss report + re-baseline run). Nối (ll) artifact-review-trước-chạy, (kk) payload bất đối xứng theo domain, (hh) injection dataset-aware, (gg) token-discipline; memory: prompt-memory-design-is-first-class.
- **2026-06-15 (ll)** — **Prompt/context/cost = ARTIFACT NGHIÊN CỨU BẮT BUỘC review TRƯỚC khi chạy + cost là tiêu chí GATE + Literary Builder = continuity LỌC-THEO-WINDOW (Option C, không full-dump không zero) (chốt 3 bên: user phê bình results-forward + nêu lãng phí token bơm nhân vật-C/glossary-G2 khi window chỉ A/B/G1; CodeX đề `LiteraryBuilderContextPack` + 6 mục per-task; Claude xác minh `compress()` ĐÃ cap → tái khung "bounded-by-size, KHÔNG-by-relevance").** **(1) Prompt = THIẾT KẾ MEMORY-CONTEXT của mỗi agent = lõi đóng góp khóa luận (context tối ưu: đủ vừa, không dư không thiếu) — KHÔNG phải chi tiết triển khai để chạy vội lấy số.** Trước mọi run dựng/đổi prompt hay injection PHẢI surface cho user: (a) mẫu prompt thật gửi đi, (b) ước lượng token/call + tổng, (c) xác nhận injection anchored/bounded, (d) hành vi cache. User chỉ ra Claude+CodeX results-forward suốt từ đầu (chưa từng xuất prompt, chưa bàn cache) → DỪNG kiểu đó. **(2) Cost là tiêu chí GATE, không phải số báo cáo suông: "S1 hơn S0 1 điểm nhưng chi phí gấp 10 = KHÔNG ĐẠT" — thành công = chất-lượng-TRÊN-token (cân bằng cost↔quality), không phải chỉ S1>S0.** Lãng phí nhỏ/call × hàng nghìn call trên sách dài = lớn; target = văn bản siêu dài. **(3) Literary Builder: KHÔNG full `REGISTRY_SO_FAR` (Option A) NHƯNG cũng KHÔNG bỏ-registry như D2L → Option C.** Khác D2L vì Builder văn học cần entity cũ/alias/narrator/relation+xưng hô trước để GỘP đúng + nối coreference xuyên chương → bỏ hẳn registry sẽ tạo duplicate/đứt alias. **Tái khung kỹ thuật CHÍNH XÁC (Claude xác minh `pipeline/prepass/registry.py`): `compress(max_tokens=600)` ĐÃ cap (`max_chars=max_tokens*4`; `append_capped` break khi vượt) → KHÔNG phải bug-cháy-quota như D2L (D2L nổ 2.5M vì registry thuật ngữ hàng nghìn entry); vấn đề THẬT = bound theo KÍCH THƯỚC chứ KHÔNG theo RELEVANCE-window → token thừa (bơm entity C/glossary G2 khi window chỉ A/B/G1) + có thể RỚT item liên quan khi budget đã bị item-vô-quan ăn hết. Đây là tối-ưu-relevance + vệ-sinh-tái-lập, KHÔNG phải vá khẩn cấp.** **(4) Builder vs Translator (cùng nguyên tắc anchor, KHÁC mục tiêu):** Translator cần memory để DỊCH ĐÚNG tại chỗ (anchor injection đã đúng hướng — `context_builder.plan_anchors` chỉ bơm term/entity có surface trong window); Builder cần memory để KHÔNG tạo duplicate + nối entity/relation + nhận alias/motif quay lại → Builder = filtered continuity, không full không zero. Áp pattern anchoring của Translator sang Builder. **(5) `LiteraryBuilderContextPack` (CodeX đề, Claude nhận):** Matched entities (chỉ entity có surface/alias trong window/chapter) + Matched glossary (chỉ glossary có source surface trong text) + Active relations (chỉ khi cả 2 entity xuất hiện HOẶC dialogue/narrator cần xưng hô) + Narrator card (LUÔN nếu first-person — continuity quan trọng) + Recent carryover (lượng nhỏ top-K/last-active từ chương trước để bắt alias mới) + Budget cap 300–600 token có `dropped_by_budget` + Audit log (`included`/`excluded`/`matched_by`/`dropped_by_budget`/`token_estimate`). **(6) MỌI task có LLM-call dài PHẢI có 6 mục TRƯỚC khi chạy full (thiếu bất kỳ mục = chưa được chạy):** `Representative full prompt` (≥1 prompt thật/render đầy đủ) · `Context inclusion policy` (đưa gì, loại gì) · `Token budget` (system/user/context/source/output) · `Cache plan` (prefix ổn định nào, kỳ vọng cache, cache-key gồm gì) · `Stop condition` (vượt ngưỡng thì dừng) · `Cost-quality report` (quality gain kèm token/cost multiplier). **(7) prompt-version PHẢI bump khi byte đổi** (vệ-sinh tái-lập; gốc: audit TI thấy Translator drift em-dash→hyphen mà version không đổi → số vẫn đúng nhưng vi phạm reproducibility). Nối (gg) token-discipline, (hh) injection dataset-aware, (ii) scope=scope, (kk) một-engine-nhiều-profile; memory: prompt-memory-design-is-first-class, token-growth-halt-and-audit, scoring-scope-equals-production-scope. **Áp dụng: TASK HYG-01** (5 bước vệ-sinh TI: bump translator v2 → LiteraryBuilderContextPack v2 → render 2–3 prompt thật cho user review → preflight token/cost → CHỈ SAU ĐÓ mới re-baseline S0/S1 hoặc chạy S2/S3).
- **2026-06-14 (kk)** — **Định vị khóa luận: lõi = dịch văn bản DÀI bằng memory có cấu trúc; một engine + profile; độ sâu tầng + payload + thước BẬT/TẮT-theo-domain (chốt 3 bên user/CodeX/Claude — định hình toàn luận văn).** **Lõi (KHÔNG phải kiến trúc dịch-văn-học):** bài toán = văn bản dài → model cửa-sổ-trượt TRÔI quyết định phụ-thuộc-ngữ-cảnh đã chọn trước; memory/retrieval là thuốc. Thuật ngữ (kỹ thuật) và nhân vật/xưng hô/motif/giọng kể (văn học) = CÙNG MỘT bệnh, khác biểu hiện. **Văn học = case KHÓ NHẤT (giàu ngữ cảnh, đổi theo diễn biến); D2L = case KIỂM CHỨNG SẠCH NHẤT (có gold người).** Văn học là MỘT instance, không phải mục tiêu riêng (CodeX chỉnh câu Claude nói quá tay). **Một engine, nhiều profile, độ sâu BẤT ĐỐI XỨNG theo domain:** S0→S1→S2→S3 xây MỘT lần (chung); `document_profile` quyết tầng nào active + payload + base prompt + filter + thước. D2L: S1 hard-terminology THIẾT YẾU (đã chứng minh sửa trôi: D 0.59→0.70, B 0.76→0.83), S2 nhẹ (referent this/above) nếu cần, S3 narrative/motif KHÔNG đáng tiền. Văn học: S1 chỉ là nền, S2/S3 (rolling context, entity state, address policy, motif, narrative brief) mới là giá trị thật. **Không chỉ bật/tắt — tầng bật mang PAYLOAD khác theo domain (Claude): S2/S3 cho D2L nếu làm = translation-memory THUẬT NGỮ, KHÔNG bolt literary-S2/S3 vào rồi kết luận vô dụng = ablation rơm; D2L D=0.70 còn headroom.** **Giá trị biên mỗi tầng phụ thuộc thể loại = ĐÓNG GÓP cần ĐO, không phải điểm yếu.** **Schema CHUNG + sparse/optional:** D2L để trống `entities`/`entity_relations`/`motif` (snapshot entities=0); chi phí ~0 nếu runtime không dump bảng rỗng; vấn đề không ở schema CÓ bảng mà ở profile có BƠM vào Translator không. **Thước = khung CHUNG, instrument theo domain (CodeX):** Correctness (D2L=B/TAR-vs-gold; văn học=judge adequacy vì không gold exact) / Consistency (D2L=D thuật ngữ; văn học=entity/address/motif/term) / Fluency-style (D2L phụ; văn học chính) / Cost-reproducibility (cả hai). **`Consistency (D)` = trục CHỊU TẢI XUYÊN-DOMAIN DUY NHẤT (Claude):** B vs judge khác thang không vẽ chung; D đo được ở CẢ HAI → đặt D2L↔TI trên cùng biểu đồ chứng minh claim tổng quát → D làm trục chính nối hai track khi báo cáo. **Framing chữ nghĩa (CodeX, defensive — quan trọng):** KHÔNG gọi văn học là `kể lại` (dễ bị hiểu phóng tác/adaptation → bắt lỗi không-trung-thành). Nói: bảo toàn NGHĨA + GIỌNG, cho phép tái cấu trúc câu tự nhiên tiếng Việt, KHÔNG word-by-word, KHÔNG phóng tác — khớp doctrine bản-người = adaptation nên hệ phải trung-thành+tự-nhiên. **Scope: CHỐT 2 dataset** (D2L kỹ thuật ↔ TI văn học kẹp cả phổ); tài liệu thường = 1 câu future-work, KHÔNG build profile thứ 3 (chống phình). **Câu khóa luận:** xây + đánh giá pipeline dịch EN–VI cho văn bản DÀI, dùng memory tự sinh từ nguồn để cải thiện nhất quán + kiểm soát ngữ cảnh; đánh giá trên hai miền kỹ thuật (D2L, controlled/committee-grade) + văn học (TI, advanced, chứng minh vì sao cần S2/S3). Nối/củng cố (ii) document_profile, (dd) 4-thước dual-headline, (ee) knob ceiling/gap, (hh) injection dataset-aware.
- **2026-06-14 (jj)** — **P3-D2L chạy STAGED + caption→passthrough a-priori + sai-phân-loại-KHÔNG-hỏng-B/D (chốt 3 bên; user đề xuất pilot-trước-full, Claude thêm guard chống tuning-on-test + bất biến validity).** **(1) Staged gate (thay chạy-full-ngay):** Stage 0 đóng băng instrument → Stage 1 offline tests + preflight CẢ 4 chương (0 API) → Stage 2 VALIDITY-PILOT 1 chương `preliminaries` (224 code block nhiều nhất → stress-test code-có-lọt-Translator / scorer-có-đếm-nhầm) → Stage 3 full 4 chương MỘT lần. **EXIT Stage 2 = CƠ HỌC** (0 block code/math/image/label vào window; scorer loại passthrough khỏi mẫu; preflight/ceiling kích hoạt + prompt/call không phình; injection occ≥2+role+canonical, preserve loại; audit tay 5 mẫu passthrough), **KHÔNG phải B/D magnitude** — pilot in B/D để xem trước nhưng cấm dùng làm điều kiện pass. Pilot `preliminaries` (instrument frozen) = bản benchmark THẬT của chương → Stage 3 tái dùng cache (0 token). **(2) Caption a-priori (chống tuning-on-test, nối ee):** quyết TRƯỚC khi nhìn số: caption `image`/`label` = PASSTHROUGH cho P3; scope đo = thân bài (heading+prose); dịch caption = completeness → usability track sau. CodeX từng đề xuất refine-profile-sau-pilot nếu thấy caption quan trọng → Claude chỉnh: đổi thước dựa trên số benchmark = luyện-trên-đề-thi, phải quyết a-priori. **(3) Bất biến VALIDITY (Claude, làm rõ doctrine scope=scope):** vì bộ lọc dịch + scorer cùng đọc CÙNG cột `block_type`, scope_dịch ≡ scope_chấm theo CẤU TRÚC → `classify_block` (rule-based first-line, `d2l_markdown_loader.py:197`) bin sai CHỈ ăn vào completeness (caption sót) + nhiễu nhỏ, KHÔNG sinh trần giả <1.0 → headline dung sai cho khâu gán nhãn; sai phân loại = vấn đề usability layer-2, không phải lỗi số. **(4) Nếu sau P3 cần NHÌN B/D rồi chỉnh CƠ CHẾ (gap) → phải trên DEV `deep_learning_computation`, không trên benchmark; P3 không rơi ca này vì injection đã khóa bởi (hh).**
- **2026-06-14 (ii)** — **`document_profile` KHÔNG fork + base prompt D2L là CEILING per-dataset + scope-chấm = scope-dịch (chốt 3 bên hội tụ; CodeX đề xuất profile, Claude thêm scope-match; user khởi từ câu hỏi "prompt TI có hợp D2L không").** **Vấn đề:** base prompt Translator hiện tại tune VĂN HỌC ("literary translator / Newmark V / DIALOGUE communicative / storytelling / carry over ship names") → SAI register cho D2L (kỹ thuật, không thoại, không ẩn dụ); không chỉ thừa token mà sai mục tiêu dịch. **Giải (KHÔNG fork pipeline):** thêm `document_profile` = config KHAI BÁO gói {base_prompt, block_filter, injection_policy}; engine (windowing/cache/runner/memory-pack/scoring/logging) DÙNG CHUNG. Hai profile khóa luận: `literary_v1` (TI) + `technical_d2l_v1` (D2L) — KHÔNG xây hệ "đa thể loại" lớn (YAGNI; 2 dataset là đủ + CHÍNH là bằng chứng tổng quát). **Guardrail (Claude):** profile là DATA, một code path; CẤM `if profile==…` rải trong logic dịch/chấm = fork trá hình. **Base prompt = CEILING (nối ee "prompt không monolithic"):** đặt ĐÚNG NGAY S0, KHÔNG có khái niệm "prompt hoàn chỉnh dần ở S3/S4" (sửa cách nghĩ user — CodeX+Claude đồng thuận); S1/S2/S3 = THÊM TẦNG memory đo được, không phải prompt to dần. **S0 và S1 D2L dùng CHUNG base prompt `s0_d2l_v1`/`s1_d2l_v1`, khác DUY NHẤT khối injection** → gap S1−S0 sạch. D2L base: register expository, giữ inline code/ký hiệu/units/citation (`.shape`/`16kHz`), bỏ luật thoại. `purity_check` mở sang profile D2L (S0 sạch gold/term). **Block filter (profile):** D2L dịch `heading+prose`; `code/math_block/image/label` PASSTHROUGH nguyên văn (heading vào vì chứa thuật ngữ + là phần bản dịch sách). **SCOPE = VALIDITY (Claude thêm, chưa bên nào chốt):** corpus chấm B/D = ĐÚNG tập block đã gửi Translator (heading+prose); loại block passthrough khỏi CẢ tử lẫn mẫu — nếu không, gold-term nằm trong code/caption (CỐ Ý không dịch) kéo mẫu lên → bản dịch hoàn hảo vẫn <1.0 = trần giả (phạt Translator vì không dịch đoạn ta bảo passthrough). Nguyên tắc tổng quát: **mẫu của một thước phải = phạm vi hệ THỰC SỰ tạo output.** **Token (nối gg):** tối ưu prompt KHÔNG phải đòn tiết kiệm token chính; đòn chính = không-dịch-code/math + không-bơm-preserve/hapax + không-dump-registry + preflight. **Áp dụng:** TASK_P3_D2L fold {profile technical_d2l_v1, base prompt D2L, block filter heading+prose, injection occ≥2+role canonical-only, scope-match scorer, preflight + `--preflight-only`, `prompt_token_cap` trong llm_translate.yaml, purity_check D2L}.
- **2026-06-14 (hh)** — **Injection policy: occ≥2 dataset-aware + derive `injection_role` trong code (chốt 3 bên hội tụ user/CodeX/Claude, khởi từ câu hỏi user về hapax).** Builder GIỮ HẾT term + đếm occurrence (cho audit / C-diagnostic / full-book sau); lọc ở **tầng INJECTION, KHÔNG ở registry** (tách "biết gì" khỏi "ép gì"). **D2L (kỹ thuật): inject nếu (term có trong window AND `occurrences_count`≥2).** KHÔNG exception rộng cho `do_not_translate`/`code_api`/unit/abbreviation — các token này Translator GIỮ NGUYÊN, không dịch → bơm = phình vô ích (user phát hiện qua `16kHz`/`.shape`/`PyTorch`; CodeX đã rút lại list rộng). Thêm exception TỐI THIỂU (term Builder gắn `forbidden_variants`/risk = hay bị dịch sai) CHỈ khi đo S1 thấy hapax dịch sai THẬT (YAGNI: đo trước, thêm sau). **Văn học (TI): inject nếu (trong window AND (occ≥2 OR entity/`entity_relations`/motif/proper_noun)).** KHÔNG áp occ≥2 thuần cho truyện — tên riêng/vật phẩm/địa danh/xưng hô hapax vẫn mang nghĩa + thuộc arc thực thể; failure-mode khác D2L. **Registry TRỘN 3 loại** (translate-term / preserve-token / reference-phrase) → con số 1608-vs-163 là LỆCH PHẠM VI + trộn-loại, KHÔNG phải tín hiệu chất lượng thuần. Phân loại bằng **`injection_role` derive trong CODE** từ `term_type`+`do_not_translate` (CHƯA migrate schema — chỉ tách bảng nếu sau P3 thấy rối thật). **Report P3-D2L (bắt buộc minh bạch):** raw_registry / translation_eligible / preserve_count / hapax_dropped / injected_per_window / flat_recall (vs all gold = Builder quality) / recurring_recall (occ≥2 = consistency thật). Nguyên tắc nền: **giá trị một mục memory ∝ số lần tái xuất** — hapax = "biết là đủ, không ép". Số chốt scorer: matched 121 = 96 recurring + 25 hapax (ad-hoc query lệch +2 do thiếu present-filter — headline LẤY TỪ SCORER).
- **2026-06-14 (gg)** — **QUY TẮC THƯỜNG TRỰC: kỷ luật token/quota (chốt theo yêu cầu user; áp cho MỌI tier hiện tại + Agent tier tương lai, KHÔNG riêng D2L).** (1) **Tăng siêu tuyến → DỪNG:** nếu prompt/token mỗi call phình theo chương/window/tier một cách bất thường, DỪNG và truy nguyên nhân TRƯỚC khi đốt tiếp; cấm "chạy cho xong". (2) **Quota ≠ chi phí $ (hai đồng hồ khác nhau):** quota token/ngày đếm cached input ở MỨC ĐẦY ĐỦ; $ giảm cached ~10× (+ incentivized tier gần như free). → ĐO quota bằng SỐ TOKEN, KHÔNG suy từ $ (vd run \$0.25 nhưng 2.74M token vượt trần 2.5M). (3) **Guard quota theo UTC**, không `date.today()` local (OpenAI tính UTC). (4) **Hai lưới đỡ bắt buộc trước run dài:** (a) *preflight estimator* = số_call × token_TB, in ra + chặn nếu vượt ngưỡng quota; (b) *per-call ceiling* = client ABORT nếu prompt_tokens một call vượt ngưỡng tier → bắt bất thường ngay call ĐẦU, không đợi đốt hết quota. (5) **Prompt mỗi call ĐÃ được lưu** ở `*_cache.sqlite3` → `llm_call_cache.request_json` (+ usage_json, cost_usd) — dùng audit/truy bloat; cache là gitignored (transient) nên khi chốt PASS một benchmark, COPY cache + report ra artifact audit. (6) **Trước khi chạy lại bản ĐÃ-SỬA, copy dữ liệu run cũ ra `_baseline/`** để so sánh trước/sau (vd marker v6 trước fix). Ổ áp dụng đầu tiên = (ff) D2L Builder registry-bloat.
- **2026-06-14 (ff)** — **D2L Builder KHÔNG bơm registry vào extraction window + mô hình chi phí chính xác (chốt sau hội tụ user/CodeX/Claude về token-overrun P2-D2L).** Bug: `registry_so_far` (full glossary, `compress()` không cắt glossary — registry.py) bị nhét vào MỌI window → input nổ. **Sửa (đồng thuận 3 bên):** D2L extraction window **KHÔNG mang registry** — mỗi window trích ĐỘC LẬP; nhất quán cuối do **consolidation/Span Resolver** (gộp theo source-term, chọn canonical theo tần suất) bảo chứng, KHÔNG cần model "nhớ" lúc trích. Lợi thêm: window độc lập giữ được occurrence/evidence (model không bỏ re-emit term "đã có"). Registry full chỉ dùng cho consolidation/persist. `compress()` phải cắt cả glossary (TI/future không dính lại). Quota guard `date.today()` local → **UTC** (validity, LOCK ee). **Mô hình chi phí ĐÚNG (sửa "O(window²)" hớ của Claude — CodeX chỉnh):** `registry_text` tính 1 lần/chương (`runner.py:288`), HẰNG trong chương, chỉ phình GIỮA chương → bậc thang per-chapter × windows-in-chapter, superlinear theo SỐ CHƯƠNG không phải per-window (bằng chứng: registry chars dev 32 → MLP 36,837; prompt/call 1.2k→10.8k). Ước tính: code cũ ~2.0M/4 chương (sát trần 2.5M, retry là nổ); sau sửa ~0.8M. **Staging (CodeX, chốt — chống YAGNI):** (1) bỏ registry cho D2L Builder → (2) chạy DEV C-gate (recall phải ≥0.5, agent vẫn bắt) → (3) CHỈ thêm anchor-filter (lọc term-trong-window — đúng pattern Translator P4-02 `plan_anchors`) NẾU conflict/agreement tệ. KHÔNG build anchor-filter đầu cơ. **Sửa code TRƯỚC khi tạo API key mới** (key mới cùng project nhiều khả năng DÙNG CHUNG quota 2.5M/ngày, không cho quota tươi). Patch: prompt v7 bỏ registry + test chống registry-dump + preflight token estimator.
- **2026-06-13 (ee)** — **Hai lớp claim + phân loại knob + phương pháp dev/test (chốt sau phản biện CodeX, SỬA câu "chỉ chấm khoảng cách" hớ của Claude).** **HAI lớp claim, hội đồng hỏi cả hai:** (1) *Comparative* — memory hơn baseline bao nhiêu (gap S1−S0, BỀN với mức trần, chỉ cần trần cố định); (2) *Usability* — bản dịch cuối có ĐỦ TỐT không (PHỤ THUỘC mức trần tuyệt đối). Claude từng nói "không tối ưu prompt/model cũng không hỏng luận" → SAI/quá tuyệt đối: đúng cho claim 1, hỏng claim 2. **Sửa:** trần (model/base-prompt/window/temp/seed) phải **HỢP LÝ + khóa TRƯỚC benchmark**, không chỉ "hằng số"; đổi trần → chạy lại CẢ 4 arm. **Ba loại knob:** *ceiling* (cố định-đồng-đều, hợp lý) / *gap* (biến độc lập = cơ chế, ĐƯỢC tối ưu) / *validity* (sai là HỎNG SỐ không phải điểm thấp). **prompt KHÔNG monolithic:** *base-translation-prompt* = ceiling (S0/S1/S3 chung nền); *memory/context-injection-prompt* = **gap knob** (cơ chế S1/S3). **cache-key BẮT BUỘC gồm:** model, messages, temp, seed, prompt_version, response_format, base_url/provider (đã dính bug base_url ở EV-02). **DB ảnh hưởng chất lượng nếu:** sai block order/type/translation_mode, lẫn gold↔registry, freeze sai. **Builder prompt D2L = chế độ THUẬT NGỮ KỸ THUẬT** (termhood, canonical, abbreviation, do-not-translate code/API/framework, allowed/forbidden variants, definition/evidence span) — KHÁC prompt TI (nhân vật/quan hệ/motif); bê nguyên TI sang → C thấp oan → kéo B. **PHƯƠNG PHÁP (chống tune-theo-test):** tune Builder prompt + toàn config trên **1 chương DEV RỜI HẲN bộ benchmark** (vd deep-learning-computation) → **KHÓA hết config** → chạy 4 chương benchmark MỘT lần. **Usability trên D2L:** KHÔNG có bản VI người tham chiếu (repo = MT rác) → "đủ tốt" đo bằng **judge (adequacy/fluency) + human Likert ~20–30 mẫu**, KHÔNG bằng gold (gold chỉ phủ thuật ngữ); giữ ở ngưỡng "chấp nhận được", không phải SOTA. Claude công nhận ~85-90% CodeX đúng; hai bổ sung Claude: dev-chapter phải disjoint + usability buộc dựa judge vì thiếu reference.
- **2026-06-13 (dd)** — **Doctrine đo thuật ngữ D2L: 4 thước A/B/C/D, DUAL headline B+D (chốt sau phản biện CodeX, SỬA phát biểu hớ của Claude).** Claude từng nói "headline = consistency, TAR-vs-gold phụ" → SAI/over-correction (vứt bỏ ưu thế lớn nhất của D2L = có gold chấm correctness). CodeX sửa, Claude công nhận. Bốn thước, cùng "TAR" nhưng KHÁC thước → KHÁC nghĩa (doctrine z-ter "không trộn thước"): **A = TAR vs registry-Builder** (Translator nghe lời; KHÔNG gold; bão hòa như TI) — chẩn đoán; **B = TAR vs GOLD-accepted** (output đúng thuật ngữ chuẩn người, tính theo OCCURRENCE — term xuất hiện 100 lần nặng 100; chấp nhận canonical_target + allowed_variants, KHÔNG exact-only kẻo bị "chấm giống đáp án") — **HEADLINE**; **C = Builder vs gold** (artifact Builder giỏi như người không; tính UNIQUE-term: recall + agreement + **conflict-list**) — chẩn đoán; **D = Term consistency** (gold-free; có trôi thuật ngữ xuyên bản dịch không) — **HEADLINE**. **DUAL headline B+D** vì hai câu hỏi khác nhau và bù lỗ hổng của nhau: chỉ-D → gameable ("nhất quán nhưng SAI": agent→"đại lý" suốt, D cao B thấp); chỉ-B → bỏ qua trôi. **B⊥D trực giao:** D phải coi đảo qua lại giữa HAI biến thể HỢP LỆ ("tác tử"↔"tác nhân") VẪN là trôi (đúng nỗi lo GVHD) → một câu có thể đạt B mà trượt D. Truth table chẩn đoán (CodeX): A↑C↑B↑ = mọi thứ tốt; A↑C↓B↓ = lỗi Builder; A↓C↑B↓ = Builder tốt Translator cãi; D↑B↓ = nhất quán nhưng sai gold; D↓B~ = đúng gold nhiều chỗ nhưng trôi (xấu cho sách dài). **Implementation:** P1-02 `eval_glossary_gold` mới có target đơn → **P3-D2L bổ sung curate `allowed_variants` eval-side** cho term nhạy cảm/tranh cãi (agent, model, loss, inference, embedding) + xuất **danh sách bất đồng Builder↔gold** thay vì binary fail. Dịch S0/S1 rẻ (OpenAI 2.5M tok/ngày) → A/B/C/D + ECS chạy HẾT block ($0 tất định); judge tách riêng, `--sample`, cờ bật/tắt (Gemini proxy trả phí).
- **2026-06-13 (cc)** — **Nguồn D2L = MT thô (KHÔNG phải gold tham chiếu) + luật snapshot tái-lập (chốt khi mở track D2L).** (1) **Phát hiện nguồn:** repo dễ-tải `d2l-ai/d2l-vi` (pin commit c775d6b, ~214MB, gitignore — chỉ track `D2L_PROVENANCE.md`) có EN `_origin.md` = bản gốc D2L THẬT + `glossary.md` = bảng EN→VI người-chuẩn (aivivn) = **GOLD cho TAR**; NHƯNG bản VI chương là **MÁY DỊCH THÔ chưa hiệu đính** (tiếng Anh xen giữa câu; "neural network" glossary chốt `mạng nơ-ron` mà MT dùng `mạng thần kinh` SAI ở 57 file vs 17). → **SỬA giả định (aa)** "D2L có bản VI chuẩn → BLEU/COMET hợp lệ": SAI với repo này. D2L chạy **TAR (vs glossary gold) + self-consistency + judge** (giống TI nhưng có glossary kỹ thuật thật); reference-metrics (BLEU/COMET) HOÃN tới khi lấy bản người `aivivn/d2l-vn` (quyết ở EV-03). BONUS: tỉ lệ 57:17 = bằng chứng thật, công khai, định lượng cho đúng mối lo GVHD ("đầu tác tử sau tác nhân") → động lực mở bài. (2) **Luật tái-lập:** DB `memory.sqlite3` gitignore → bản dịch S0/S1 + registry (thước TAR) + evaluation_runs chỉ sống trên ổ đĩa; report `*.json` chỉ giữ SỐ, không giữ VĂN BẢN để chấm lại. **Trước khi pivot dataset / nâng cấp thước, BẮT BUỘC xuất snapshot bền vững** bằng `pipeline/scripts/snapshot_runs.py` (translations+sources+registry+evaluations, tracked) để metric mới chấm lại CHÍNH bản dịch cũ mà không dịch lại. Đã chạy cho TI: `data/reports/treasure_island_p2_snapshot.json` (162 dịch / 81 source / 22 glossary / 10 entity / 733 eval). Trạng thái thước: **TAR/ECS ổn định**; **judge CHƯA chốt** (calibrated=false + chờ lọc-block F1 + sample) → lý do snapshot quan trọng.
- **2026-06-13 (bb)** — **EV-02 judge (Gemini) PASS + 4 doctrine đo-văn-phong (chốt sau
  review pilot S0-vs-S1 ch02-03).** (1) **Block-pairwise có NHIỄU trên block rác**: b001
  source="I" = số chương La Mã, S1 dịch "Tôi" SAI nhưng judge cho thắng (không biết là
  heading) → headline chất lượng phải dùng **pairwise HOLISTIC cấp chương + lọc block dưới
  ngưỡng token tối thiểu**, không lấy thẳng tỉ lệ thắng block thô. (2) **GEMBA-direct mãi
  mãi DIAGNOSTIC, cấm headline**: pilot ra S0 70 > S1 54 đều 4 trục, NHƯNG đó là **đánh đổi
  thật** — S1 mua nhất-quán-thuật-ngữ (TAR 0.42→1.0) bằng chi phí tự-nhiên nhỏ (viết hoa
  canonical, mở rộng "quán trọ", ép dịch heading) mà TAR mù còn GEMBA tuyệt-đối thấy;
  pairwise (so trực tiếp) ra ~hòa (S1 12 / tie 59 / S0 10). Củng cố (z-ter): pairwise là
  chính. (3) **Tách bạch 2 loại chi phí**: chi phí DỊCH = chi phí sản phẩm/tài liệu (rẻ,
  gom window); chi phí ĐÁNH GIÁ judge = chi phí nghiên cứu MỘT-LẦN (cached, ~20× dịch vì
  chấm block × swap × config) — KHÔNG được cộng vào chi phí dịch khi kể chuyện "~1/100".
  (4) **Bắt buộc SAMPLE cho judge từ nay**: pilot/đo judge chỉ chấm 20–30 block sample +
  GEMBA chỉ trên sample; quét đầy đủ chỉ làm SAU hiệu chuẩn (ρ vs người) và giữa config
  thật-khác-nhau (S1-vs-S3), không S0-vs-S1 (đã biết ~hòa). Thêm `--sample N` + lọc token
  → follow-up EV-02b. Lệch spec chấp nhận: judge chạy qua proxy ShopAIKey (key `sk-`→
  api.shopaikey.com) vì AI-Studio free-tier 429; cross-provider VẪN GIỮ ở cấp model
  (gemini-2.5-flash ≠ gpt-5.4-mini); `cost_usd=0` → số chi phí proxy ≠ giá Google official.
  Mọi số judge gắn `calibrated=false`, CHƯA trích hội đồng tới khi có human ratings + ρ.
- **2026-06-13 (aa)** — **ĐỊNH VỊ GIÁ TRỊ CỐT LÕI + chia vai 2 dataset (chốt sau nhận
  định user, REVISE khung kể §1/§6.1).** Luận điểm trung tâm: *dịch văn bản SIÊU DÀI bị
  trôi thuật ngữ/tên theo độ dài (oracle mạnh vẫn drift ~11%); pre-pass đóng băng + bơm
  tất định biến nhất quán từ may-rủi → ĐẢM BẢO kiến trúc, ~1/100 chi phí.* Giá trị này
  **mạnh & đo rõ nhất trên tài liệu KỸ THUẬT** — đúng mối lo GVHD đã tự nêu ("đầu dịch
  'tác tử' sau 'tác nhân'"); TAR đo CHÍNH cái GVHD lo. Chia vai đo lường:
  • **D2L (kỹ thuật, có bản dịch VI chuẩn):** đo ĐƯỢC CẢ HAI — TAR (nhất quán, nơi kiến
    trúc tỏa sáng) + ngữ nghĩa qua tham chiếu (BLEU/chrF/COMET hợp lệ trên văn kỹ thuật).
  • **TI (văn học, human=phỏng dịch không làm gold):** self-consistency + judge; nhất
    quán đổi sang tầng khó hơn (tên + xưng hô động + mô-típ) = đóng góp MỚI.
  Hệ quả roadmap (ĐỀ XUẤT, chờ user chốt): cơ chế S0→S3 dataset-agnostic → làm nốt trên
  TI pilot rồi **KÉO P1-02 (D2L adapter) lên sớm** vì đó là track TAR có nghĩa nhất +
  tham chiếu hợp lệ + demo đánh trúng mối lo GVHD. Mục tiêu nền vẫn là "dịch văn bản
  siêu dài 100–1000+ trang"; văn học là lựa chọn vì thú vị, KHÔNG thay thế track kỹ thuật.
- **2026-06-13 (z-ter)** — **DOCTRINE TAR (chốt sau phản biện của user, REVISE §6.2 cách
  dùng TAR).** Số bằng chứng matched-scope: oracle chấm vs TỪ-ĐIỂN-CHÍNH-NÓ trên ch02-03
  = ch02 0.929 / ch03 1.0 (không phải 0.62 — 0.62 là phí thước-lạ). 2 builder cho 2 từ
  điển khác: 8 term trùng → 4 lệch VI (cutlass: thesis "kiếm cong" vs oracle "mã tấu" —
  oracle hợp quy ước hơn mà vẫn bị TAR-thesis phạt 0). Chốt:
  (1) **Vai trò TAR = adherence tới MỘT thước cố định.** Headline hợp lệ DUY NHẤT =
     S0→S1 cùng thước thesis (42%→100%) = bằng chứng cơ chế injection chạy. CẤM dùng
     TAR thesis-vs-oracle như điểm chất lượng (thước lạ).
  (2) **Cách A (mỗi hệ vs từ điển riêng = tự-nhất-quán):** S1 1.0 vs oracle ~0.93-1.0
     (matched scope) → khoảng cách NHỎ trên pilot; cơ chế khác (S1 obedience-injection
     vs oracle coherence-2-pass) + denominator khác (22 vs 470 term) → so suy diễn, KHÔNG
     airtight. Giá trị thật injection = "đảm bảo kiến trúc vs ~93% may rủi, NỚI theo độ
     dài sách" — chỉ chứng minh được bằng scale P6, CẤM tuyên bố sớm.
  (3) **Cách B (vs thước người trung lập):** đo CHẤT LƯỢNG TỪ VỰNG của Builder (vì S1
     translator chỉ chép builder), KHÔNG đo văn phong/"hay như người" (đó là judge).
     allowed_variants hiện =[target] match cứng → Cách B BẮT BUỘC nới synonym kẻo phạt
     oan. **Cách B chỉ dùng D2L** (bản dịch trung thành + glossary gold); TI = phỏng
     dịch, không làm gold được.
  (4) **Tách trục tuyệt đối:** TAR = nhất quán thuật ngữ; judge/COMET = đúng nghĩa/hay.
     Không trộn. → củng cố EV-02 (judge) trước S2/S3.
- **2026-06-13 (z)** — **TAR BÃO HÒA ở S1** (phát hiện khi review P4-02, ảnh hưởng cách
  đọc cả thang ablation §6.2). Số pilot: S0 TAR 0.4151 → S1 1.0 → vượt oracle 0.6226
  (cùng 53 pairs). TAR=1.0 là hệ quả cấu trúc, KHÔNG phải chất lượng dịch: provider
  span_resolver chấm đúng tập term có source trong window, context builder bơm thẳng
  target đã duyệt cho chính tập đó → tuân lệnh = 1.0 gần tất yếu. **Hệ quả chốt:**
  (i) TAR = thước chứng minh "injection ăn tiền" (S0→S1 + vượt oracle), KHÔNG dùng để
  phân biệt S1/S2/S3 (đều ~1.0); (ii) phân hóa S1→S3 đo bằng **ECS** (pilot 0.7556→
  0.8111, còn dư địa) + **semantic judge/COMET** (kéo eval đa trục P5 lên sớm) +
  **case study arc xưng hô** (cần chương dài, cặp có đổi pha); (iii) caveat báo cáo:
  1.0 là trần pilot (53 pairs, dropped=0), quy mô sách sẽ tụt. Giá trị address policy
  chỉ lộ ở cặp ĐỔI pha (Jim↔Silver) — pilot 2 chương captain↔doctor "ông"/"ông" nên
  S1≈S0 ở thoại đó.
  **(z-bis) BẤT ĐỐI XỨNG ĐÁP ÁN — CẤM trình bày "S1 > oracle = dịch giỏi hơn":** S1
  được PHÁT registry (đáp án) rồi chấm bằng chính registry; oracle bị chấm bằng thước
  NÓ CHƯA TỪNG THẤY (oracle tự xây glossary riêng). Ví dụ cutlass: thesis chốt "kiếm
  cong" → S1 bơm→trúng 1.0; oracle tự dịch "kiếm quắm" (CŨNG ĐÚNG) → lệch chuỗi
  registry → 0.0. Oracle bị phạt vì KHÔNG BIẾT ta chọn chữ nào, không phải vì dịch sai.
  → "S1 vượt oracle trên TAR" = cầm phao, KHÔNG phải chất lượng. Triệu token oracle đổ
  vào thứ TAR mù (văn phong/ngữ nghĩa — xem b006 oracle mượt hơn). Khung kể đúng cho
  GVHD/hội đồng: "memory per-book cho nhất quán thuật ngữ/tên gần tuyệt đối với ~1/100
  chi phí; trung thành ngữ nghĩa đo RIÊNG bằng judge/COMET, ở đó khoảng cách có thể
  đảo chiều". Thiên vị đã nhận diện: (a) registry xây từ chính chương test = train-on-
  test cho trục thuật ngữ; (b) cùng model family. Dự đoán sách khác: TAR S1 vẫn ~1.0
  (hằng số vòng kín, không lộ mặt qua TAR); chỗ lộ mặt = ECS + judge ngữ nghĩa. →
  CỦNG CỐ quyết định kéo eval đa trục (judge) lên trước S2/S3.
- **2026-06-13 (y)** — Chốt quan hệ cuối cùng app ↔ pipeline (user xác nhận hướng):
  **app = COCKPIT, pipeline = ĐỘNG CƠ** — app gọi pipeline (subprocess/API) + đọc
  DB/artifacts, VĨNH VIỄN không hút code (tái khẳng định (x)); xương sống app dịch
  chuyển sang chế độ chạy end-to-end tự động, **human edit = OPTION có nhãn**: run có
  bàn tay người phải đánh dấu riêng, KHÔNG BAO GIỜ vào số benchmark (ràng buộc GVHD
  §0); memory frozen miễn nhiễm human edit nhờ trigger 004. UI observability (hiển thị
  prompt từng call / cache hit / DB / tiến trình) = ĐỌC từ những gì pipeline đã log
  sẵn by design: llm_call_cache.request_json, memory_packs.payload_json
  (zones+token_breakdown), usage_daily, translation_runs, reports. UI task riêng SAU
  P4 (theo (w)); P4 đi trước để demo có cái mà chiếu.
- **2026-06-12 (x)** — Chốt nguyên tắc reuse sau câu hỏi "sao không chỉnh app/ mà xây
  pipeline/ riêng": **tái sử dụng qua ranh giới DỮ LIỆU (schema SQLite, canonical
  document.json, memory.sqlite3 chung), KHÔNG import code web tool vào runtime** —
  app/ có xương sống human-in-the-loop (routes annotation→review/edit→package, audit,
  history) trái với Directional Lock; pipeline/ cần headless + tái lập + ablation-sạch.
  Case cụ thể: `annotation_flow._resolve_surface` (app) resolve TỪNG candidate kèm
  context, mơ hồ → người xử; thesis cần liệt kê HẾT match của surface, không người →
  span_resolver.py VIẾT MỚI (~100 dòng, tái dùng triết lý matching consistency.py),
  mượn test case từ app. Điểm nối hai hệ = cùng DB (UI đọc DB pipeline ghi — xem (w)).
- **2026-06-12 (w)** — Xác nhận lại định vị reuse (user hỏi): pipeline/ = bản TỰ ĐỘNG
  HÓA của vòng thủ công AI-LAB (skill+prompt chat → prompt.py+LLMClient; mắt người soi
  JSON → validator+re-ask; import tay → loader/persist; "chốt" tay → FREEZE trigger).
  **UI `app/` tái dùng có chỉnh** — task riêng SAU P3 (khi có translation_runs để hiển
  thị): trỏ vào memory.sqlite3 thesis (cùng schema), thêm view translation_runs/
  entity_relations/qa_issues, flow sửa annotation thủ công bị FREEZE trigger chặn ở
  tầng DB → UI chỉ còn vai trò inspect + demo GVHD/hội đồng.
- **2026-06-12 (v)** — **Xác minh API thật gpt-5.4-mini** (probe trong review P2-01,
  REVISE tham số §2.2): (i) `reasoning_effort: minimal` KHÔNG tồn tại — tập hợp lệ
  {none, low, medium, high, xhigh}; (ii) `temperature` tùy chỉnh CHỈ hợp lệ khi
  `reasoning_effort=none` (reasoning bật ≥low → ép temperature mặc định 1.0). Chốt:
  **pre-pass World Builder = low + temp 1.0** (trích xuất là việc phân tích, ~180
  reasoning tokens/chương đáng giá); **Translator/Narrative/Critic T2 = none +
  temp 0.3** (dịch cần sampling ổn định hơn cần reasoning — giữ tinh thần "minimal"
  cũ). Đã sửa `llm_default.yaml` (minimal→none). Tái lập: temp 1.0 ở pre-pass chấp
  nhận được vì replay cache + seed mới là cơ chế tái lập thật (§5.1). Lưu ý thêm từ
  probe: response_format json yêu cầu chữ "json" xuất hiện trong messages (prompt
  hiện có sẵn).
- **2026-06-12 (u)** — **REVISE đơn vị dịch: 1 call = 1 WINDOW** (chuỗi block liền kề,
  ngân sách token nguồn ~1.000–1.500 🔶 pilot quyết, min 1 block, không cắt block,
  không vượt chương, không băm chuỗi thoại vừa budget, cắt tại trigger đổi pha) thay
  "1 call = 1 block" (user đề xuất, Claude chấp nhận + ra quy tắc biên). Output JSON
  keyed block_id; **lưu trữ/metric/Critic GIỮ per block** (thêm cột `window_id` vào
  translation_runs ở P3). Lý do: Zone 1+2 lặp mỗi block → window trả 1 lần (input/block
  giảm ~3×, call giảm ~4–6×); mạch thoại không bị băm (giả thuyết chất lượng, pilot đo);
  giảm confound so với oracle dịch nguyên chương. Áp dụng đồng nhất S0→S3. Caveat đã
  phân tích: ví dụ "chèn câu dẫn thoại" của bản dịch người là ADAPTATION (đổi cấu trúc
  nguồn — ngoài scope theo (s)); window KHÔNG nhằm tái tạo điều đó, chỉ nhằm cohesion
  trong phạm vi semantic translation. Đã sửa §2 sơ đồ + bảng quyết định + state machine,
  §5 thêm mục ĐƠN VỊ DỊCH + bảng budget theo window.
- **2026-06-12 (t)** — Review góp ý retrieval từ agent ngoài, quyết định: (i) **BÁC
  `entity_presence_log`** (bảng mới theo dõi entity vắng mặt lâu) — thừa vì translator
  STATELESS per block, phase active inject theo `order_index` lookup nên block N luôn
  nhận đúng state hiện hành bất kể entity vắng bao lâu; presence per block đã nằm sẵn
  trong bảng `mentions` (gap = 1 query, không cần bảng); inject "last known address=cũ"
  còn PHẢN TÁC DỤNG vì đưa dạng xưng hô cũ vào context vốn không chứa nó. (ii) **BÁC
  inject qa_issues vào prompt block kế** — vi phạm trực tiếp CẤM §2.1 "Translator thấy
  issues block khác" (chốt có chủ đích: negative example dễ lan lỗi; hard constraint đã
  mang sẵn dạng đúng). Concern gốc (lỗi chưa repair lan qua recent window) là THẬT →
  chốt phiên bản hẹp: **recent-window hygiene** — `get_recent_translations` loại/đánh dấu
  block có issue open major/critical (1 rule trong Context Builder, 0 đổi schema, 0 đổi
  prompt). Ghi vào spec P4. (iii) Ahead-window: hoãn, quyết bằng số pilot (đồng thuận).
- **2026-06-12 (s)** — Tiếp nhận **Newmark (1988) làm neo lý thuyết dịch thuật** (qua
  Đoàn Thuý Quỳnh 2020, `reference/papers/4611-...pdf` — bản trình bày VN của sơ đồ
  chữ V 8 phương pháp). Ứng dụng chốt: (i) human ch1 = PHỎNG DỊCH, oracle = trung
  thành/ngữ nghĩa → gap đêm (r) = hai vị trí trên chữ V, không phải chênh trình độ;
  (ii) style policy Zone 1 viết bằng từ vựng Newmark: mặc định semantic, cho phép
  communicative trong thoại, CẤM word-for-word/literal (nguồn calque), KHÔNG adaptation;
  (iii) 2 track khớp kê đơn Newmark: D2L=informative→communicative (ref-based metrics
  có nghĩa), văn học=expressive→semantic (preference/judge) — đồng hình với §6.2;
  (iv) Interpretation Brief `translation_strategy` dùng nhãn Newmark; Critic T2 chấm
  "lỗi trôi phương pháp"; (v) Chương 4 thêm phân tích phân loại Newmark trên mẫu
  ~50-100 câu (người vs oracle vs S3) — template = methodology bài báo này;
  (vi) scope defense: adaptation ngoài phạm vi CÓ CHỦ ĐÍCH (đặc quyền người + phá
  measurability). Caveat: bài báo về tiêu đề ca khúc n=65 — dùng làm exposition +
  template, không làm bằng chứng cho tiểu thuyết. Citation cần truy thêm: Lê Hùng Tiến
  (2007) "Vấn đề phương pháp trong dịch thuật Anh–Việt", Tạp chí KH ĐHQGHN 13(1).
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
