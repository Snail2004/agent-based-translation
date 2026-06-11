# THESIS — AGENT FILL-POLICY CHO SCHEMA AI-LAB 1.5.0

> **Scope:** thesis-side. Cross-ref `DATASET_DESIGN.md` §5 (schema) + §6.0b/c/d
> (ownership / dataset-vs-runtime / agent-fail). KHÔNG đưa vào `AILAB_HANDOFF`.
> **Schema AI-LAB hiện là 1.5.0 và ĐÃ KHÓA** (entity_relations là thay đổi field cuối;
> không thêm/bớt field nữa). Doc này chỉ định nghĩa *agent điền field nào, khi nào,
> với chi phí token ra sao* — bản mirror "agent-as-annotator" của §6.0b (vốn viết cho
> người + AI-draft + human review).

## Tiền đề (GVHD chốt + user reframe)

- Thesis **kế thừa nguyên schema AI-LAB 1.5.0**. Khác AI-LAB **chỉ ở executor**:
  AI-LAB = người annotate (AI nháp, người chốt); thesis = **agent tự điền, không người**.
- Bài toán: schema **đủ để agent dịch tốt** nhưng **không dư** → mỗi field dư = agent
  phải sinh thêm = rộng việc + tốn token khi chạy AI 100%.

## Kết luận 1 — Schema 1.5.0 đã khóa (minimal-sufficient)

- Required tối thiểu đã gọn: block `{block_id, order_index, block_type, source_text,
  clean_text}` + glossary `{source_term, expected_target, occurrences, status}` +
  entity `{canonical_source, canonical_target, entity_type, mentions}` + chapter
  `{summary_source, source}`. Field diễn giải đều optional/nullable sẵn.
- §6.0d đã lường token-cost (narrative optional, chunk theo chapter, MVP nhỏ, subset).
  → đòn bẩy đã có trong design, không cần cắt field.

## Kết luận 2 — Agent ghi vào RUNTIME memory, KHÔNG ghi đè file gold AI-LAB

- File dataset AI-LAB (`document.json`, `glossary.jsonl`, `entities.jsonl`,
  `chapter_summaries.jsonl`, `manual_reference_subset.jsonl`) = **eval gold**, giữ
  provenance người (`source/status/annotated_by/reviewed_by`). Không đụng.
- Agent dựng memory **cùng cấu trúc field** nhưng provenance = agent/unverified.
  Khớp §6.0c (tách "dataset offline" vs "runtime memory T1-T7").
- ⇒ "kế thừa schema" = **tái dùng thiết kế field**, không phải viết vào file freeze.
- **Điểm divergence duy nhất = provenance.** `source: human|ai_assisted_verified` và
  `status: ...|human_verified|locked` không có giá trị cho agent-unverified.
  **Đã chốt:** dùng **runtime-mirror riêng** (giữ schema dataset sạch), KHÔNG nới enum
  `agent_unverified` (xem "Đã khóa" bên dưới). Quyết định này không đổi *cấu trúc* field.

## Kết luận 3 — Bảng fill-tier (đây là đòn bẩy token, không phải cắt field)

| Nhóm field (vị trí schema) | Translator dùng làm gì | Tier / ai điền | Token cost |
|---|---|---|---|
| `metadata`, `block_id/order_index/page_ids/block_type/is_chapter_opening/source_text/clean_text` | khung tài liệu + trục align | **A — code/extractor** | 0 (deterministic) |
| `sentences[].span`, `occurrences[].span`, `mentions[].span`, `provenance.raw_span` | offset | **A — code string-match** | 0 — **LLM KHÔNG đếm offset** (§6.0d) |
| glossary `{source_term, expected_target, allowed/forbidden_variants, chapter_scope}` | HARD constraint/block | **B — agent pre-pass (bắt buộc)** | vừa, 1 lần/cuốn |
| entity `{canonical_target, entity_type, gender, aliases_*, pronoun_policy}` | HARD constraint + character card | **B — agent pre-pass (bắt buộc)** | vừa, 1 lần/cuốn |
| `discourse {speaker_entity_id, addressee_entity_id, pronoun_hints}` | xưng hô (block dialogue) | **B — agent, chỉ block dialogue** | thấp |
| chapter `{summary_source, characters_present, key_events, open_threads}` | narrative memory/chương | **C — agent, 1 lần/chương** | thấp (rẻ so với dịch) |
| chapter `{setting, emotional_tone, motifs}` | SOFT advisory/chương | **C — optional** | thấp |
| block `annotations {motifs, tone, implicit_meaning, narrative_note}` | SOFT advisory/block | **D — optional, CHỈ khi block khó** | **CAO nếu điền mọi block → giữ THƯA** |
| `quality_flags`, `provenance.corrected_by/correction_note` | QC/human bookkeeping | **không agent điền** | — |
| `status=human_verified/locked`, `source`, `annotated_by`, `reviewed_by` | provenance người | **không agent điền** (xem Kết luận 2) | — |
| `manual_reference_subset.*`, `injected_errors.*`, `relevance_queries.*` (D5/D6) | eval gold | **không agent điền** (eval-only) | — |

**Nguyên tắc rút gọn:** Tier A free, B một-lần/cuốn, C một-lần/chương → tổng rẻ. Chi
phí token thực sự chỉ bùng ở **Tier D** (per-block diễn giải) → đó là chỗ duy nhất phải
siết: chỉ trigger khi block khó, không thì để `[]`/`null` (đúng §6.0d "không bịa cho đủ ô").

## Kết luận 4 — Giữ trường narrative/ẩn ý, KHÔNG bỏ (Claude + CodeX chốt)

- Câu hỏi "có nên bỏ `implicit_meaning` / thêm `metaphor`": **không, cả hai.**
- **Nguyên tắc gốc: field TỒN TẠI là free; chỉ ĐIỀN mới tốn token.** Field optional để
  `null` → agent bỏ qua → 0 token. ⇒ "bỏ field để tiết kiệm" là sai đòn bẩy; đòn bẩy
  đúng = fill-policy (sparse/optional/JIT).
- Bỏ `motifs/implicit_meaning/tone/narrative_note` sẽ **phá RQ5 / Narrative Understanding
  Agent / S3-vs-S3d** (đóng góp narrative-aware) và **D2/D6 narrative gold** của AI-LAB.
  Downside thật, upside ~0.
- Phân biệt: `motifs` = cross-block (tái diễn) → đáng điền hơn, gần HARD; còn
  `tone/implicit_meaning/narrative_note` mức block = local → JIT cho block khó, có thể
  ephemeral. Cả hai vẫn giữ slot schema.

## Tầng agent sản xuất (CodeX A–F, đồng thuận)

| Tầng | Agent phụ trách | Ghi vào schema | Bắt buộc? |
|---|---|---|---|
| A | Extractor / code | document, chapters, blocks, spans | Bắt buộc |
| B | Entity/Glossary Agent | entities, glossary, mentions, occurrences | Bắt buộc |
| C | Discourse Agent | speaker/addressee cho dialogue | Bắt buộc với thoại |
| D | Chapter Memory Agent | summary, key_events, characters_present, open_threads | Bắt buộc theo chương |
| E | Narrative Hint Agent (= Narrative Understanding, V3 §5.3) | motifs, tone, implicit_meaning, narrative_note | Optional, chỉ block khó (JIT) |
| F | Reference VI | reference_subset | AI-LAB/eval-only, không là input thesis |

## Không thêm field mới (đã đủ)

- "Main character cards" (GLOBAL core) = top entity theo **số `mentions`** → derivable,
  không cần field `importance`.
- "Style/voice card" (GLOBAL core) = `target_rendering_policy` (CONFIG pipeline,
  genre-conditioned) + `metadata.genre/domain` + suy ở runtime → **không phải field dataset**.

## Đã khóa (Claude + CodeX, 2026-06-04)

- Giữ **5 schema gốc nguyên** (không thêm `metaphor/symbolism`; không bỏ
  `implicit_meaning/motifs/tone/narrative_note`).
- Narrative/ẩn ý = **sparse optional**, agent chỉ điền khi có ích cho dịch + có evidence.
- Reference + provenance-người = **AI-LAB-only**; thesis để `null`.
- Provenance agent = **runtime mirror riêng** trong thesis; không nới enum AI-LAB bằng
  `agent_unverified`.
- **Đã thực hiện (thay đổi field cuối cùng): thêm sidecar `entity_relations` (1.4.0 →
  1.5.0)** — field hiếm hoi vượt bar "truly necessary" vì xưng hô VI (xem mục dưới). Từ
  đây schema 1.5.0 khóa, không thêm/bớt field.

## Xưng hô động — `entity_relations` 1.5.0 (3 tầng)

Xưng hô VI = `default theo pha × override theo cảnh × tính cách`, không phải 1 field tĩnh.

- **Tầng 1 — default:** `entity_relations.address_policy` (quan hệ chủ đạo của cặp).
- **Tầng 2 — diễn biến lâu dài** (thân→phản bội→thù): **nhiều record phẳng/cặp**, phân
  biệt bằng `state_label` + `valid_from_block_id` + `valid_to_block_id` (null = mở).
  `address_policy` mỗi **chiều độc lập** → bắt được lệch một phía (A leo thang `tao/mày`,
  B vẫn `tôi/cậu`).
- **Tầng 3 — bùng cục bộ** (1 câu giận): KHÔNG tạo record mới → `discourse.pronoun_hints`
  + `block.annotations.tone/narrative_note`, ephemeral.

**Precedence runtime:** `pronoun_hints > active state (theo order_index của block) >
default relation > entity.pronoun_policy > style fallback`.

**Tính cách nhân vật: KHÔNG thêm field.** Biểu diễn qua `pronoun_policy` (giọng mặc định)
+ `key_events/open_threads/emotional_tone/tone/narrative_note`. Tính cách vào quyết định
"leo thang hay kìm nén" mà Narrative/Translator agent sinh `pronoun_hint` JIT; Critic
kiểm lệch phải có evidence. (Cùng cú phản bội: nóng nảy → "Mày lừa tao!"; kiềm chế →
"Sao anh lừa tôi?" — giữ ngôi cũ là thủ pháp, không leo thang máy móc.)

**Field `entity_relation.schema.json`:**
```
relation_id, doc_id, source_entity_id, target_entity_id, relation_type   (req; relation_type free-text + recommended values)
state_label, valid_from_block_id, valid_to_block_id, trigger_event_id     (opt — cho pha; trigger_event_id = nhãn tự do, chưa có FK)
address_policy {source_to_target/target_to_source: {self_term, address_term}}  (opt)
evidence [{block_id, surface}], confidence, notes                         (opt)
```
**validate.py:** source/target/valid_from/to resolve về entity/block tồn tại; cùng cặp
cảnh báo nếu có nhiều default relation hoặc các explicit phase chồng range; default relation +
phase override là hợp lệ; so sánh theo `order_index`, không phải string.
Sản xuất bởi **Relation Agent** (pre-pass, sau Entity Agent), chỉ relation có evidence.

## Còn mở

1. Ngưỡng "block khó" kích tầng E — dialogue? nhiều entity? motif seed? Critic Tier-1
   risk? (gắn trigger Narrative Agent V3 §5.3).
2. Có lưu `confidence` agent cho field tầng B/D để sau đo memory-quality vs gold không.
