# TASK LIT-L2A — Builder văn học pilot trên Wuthering Heights (4 bước evidence-ledger, chương 1–4)

Status: **APPROVED for L2A-0 scaffold** (Claude spec + CodeX review 5 vòng hội tụ, 2026-07-08). Prompt/schema/validator 4 bước = **`design/LITERARY_PROMPT_DESIGN.md` (NGUỒN CHUẨN — CodeX scaffold bám theo file này, KHÔNG chế lại)**. Corpus ĐÃ CHỐT = **Wuthering Heights / Đồi gió hú** (pivot khỏi Treasure Island — memory `literary-corpus-wuthering-heights`, RECONCILE_V1 §4). Builder-đi-trước (như D2L): dựng Story Bible (từ điển + nhân vật + quan hệ theo pha + digest) TRƯỚC, chưa dịch/RAG/gold.
Type: BUILDER văn học — pilot đo-để-hiệu-chỉnh. Ghi **artifact JSON**, KHÔNG ghi DB. KHÔNG cần gold/oracle (spot-check bằng mắt). KHÔNG đụng Translator.
Owner: thiết kế *(Claude)*; imple *(CodeX)*; **verify gate = Claude, KHÔNG uỷ quyền** (tiền lệ E03 inert-renderer).

- **NGUỒN CHUẨN prompt/schema:** `design/LITERARY_PROMPT_DESIGN.md` — B1 lexicon (§2), B2 narrative-evidence (§3), B3 chapter-digest (§4), B4 consolidation 4-lớp+L3b (§5); nguyên tắc nền §1.5–1.9 (temporal interval/as-of, resolution-field, canary, open-interval). Scaffold PHẢI khớp schema/validator ở đây.
- **Refs cần đọc trước khi imple:** RECONCILE_V1 §0 (nguyên tắc), §2.1 (Builder chain kế thừa D2L C2→C4.5 + hoà giải timeline), §2.4 (xưng hô theo pha); memory `literary-memory-is-interval-valued-as-of-query`. Pipeline D2L hiện có: cơ chế window (`pipeline/prepass/…`), `concept_key`, memory-pack có-lọc + audit (TASK_BUILDER_V2 §3.2), Term-Auditor tầng-2 (TASK_BUILDER_V2 §3.x / memory `builder-v2-term-auditor-two-stage`). Chuẩn hoá cấu trúc nguồn: `AILAB_HANDOFF/tasks/TASK_SOURCE_STRUCTURE_NORMALIZER_PHASE01.md`.
- **Memory ràng buộc (BẮT BUỘC áp):** `code-never-does-language-work` (code chỉ cơ khí, phán đoán ngôn ngữ = LLM); `builder-v2-memory-pack-design` (pack window-anchored, KHÔNG full dump); `builder-over-merges-high-freq-head-nouns` (over-merge head-noun → mất recall — **canary WH bên dưới**); `prompt-memory-design-is-first-class`; `weighted-ledger-promotion-three-gate` (belief revision có 3 cổng); `ti-unit-id-vs-narrative-chapter` (đừng để heading-unit giả làm chương); `write-tool-unicode-path-phantom` (verify git status sau mỗi write).
- **Branch/Commit:** (điền khi imple)

## 0. Phân chặng & phân vai *(chốt 2026-07-08 — user + CodeX)*

⚠ **CHƯA có Builder văn học thật** (không như D2L Builder đã ổn). Task = **viết prototype rồi chạy**, KHÔNG phải chạy module có sẵn. Chia 2 chặng, KHÔNG chạy một mạch:

- **L2A-0 (scaffold, ~0 API):** CodeX dựng CLI `pipeline/scripts/run_literary_builder_pilot.py` (hoặc tương đương) + schema JSON từng pass + parser/validator (ép `block_id`/quote/đủ field, chặn hallucinate) + **dry-run mẫu**. Claude cắm prompt + **kiểm logic một lượt** trước khi gọi API nhiều.
- **L2A-1 (run tăng dần):** chạy **1 chương TRƯỚC** → đo cost + chất lượng → ổn mới lên 3–4 chương. Kiểm **từng bước một**, không ném chạy đầu-đến-cuối.

**Phân vai (KHOÁ):**
- **Prompt + logic nhồi window-context (4 bước) = CLAUDE viết** — đây là lõi luận văn (`prompt-memory-design-is-first-class`). CodeX **KHÔNG tự chế prompt**; chỉ được chỉnh khi run lỗi nghiêm trọng **nghi do prompt**, và **PHẢI báo cáo lại Claude**, không tự quyết.
- **Scaffold/CLI/parser/validator/chạy = CodeX.**
- **Verify logic + kết quả từng bước = CLAUDE** (không uỷ quyền, tiền lệ E03).

**Logic nhồi window-context từng bước (Claude chốt — prompt chi tiết ra file design riêng):**
| Bước | Text nhồi | Memory-context nhồi (có neo, có trần — KHÔNG full-dump) |
|---|---|---|
| 1. Lexicon | window vài block | pack-có-lọc: entry registry-so-far có surface trong window |
| 2. Narrative evidence | cùng window b1 | roster nhân vật đang trên-sân trong chương này (giải "the master"/"she" → tên) |
| 3. Chapter digest | trọn chương | rolling summary chương trước (~200 tok) + ledger b1+b2 CỦA CHÍNH chương + unresolved_threads mang sang |
| 4. Consolidation | (không text sách) | code gom candidate trước; LLM chỉ phán TỪNG quyết định điểm-huyệt + lát cắt evidence (như D2L C3/C4) |

**Kết quả Builder văn học = "Hồ sơ tác phẩm" (Story Bible)** — hồ sơ có cấu trúc + trục thời gian (T1 glossary + T2 roster + T3 dialogue map + T4 chapter digests + entity_relations timeline/xưng hô), KHÁC "từ điển phẳng" của D2L.

## 1. Bối cảnh & mục tiêu *(Claude)*

D2L một-lượt-window đủ vì thuật ngữ là thông tin **cục bộ**. Văn học thì mỗi trường memory có "đơn vị sự thật" khác nhau: thuật ngữ/tên = cục bộ; **quan hệ + pha xưng hô = xuyên chương** (không window nào thấy trọn); tóm tắt = trọn chương. Nếu bắt Builder KẾT LUẬN quan hệ trong window, nó đoán bừa từ mảnh cục bộ (đúng bẫy D2L first-write-wins).

**Nguyên tắc khoá — EVIDENCE LEDGER, không phải memory hoàn chỉnh:** output các bước window/chương **chỉ là sổ bằng chứng** (mỗi dòng kèm `block_id`). Builder **BỊ CẤM** kết luận quan hệ/pha ở tầng window. Mọi kết luận (danh tính nhân vật, timeline pha, canonical term) chỉ sinh ra ở **Bước 4 consolidation**.

**Mục tiêu pilot (chương 1–4 WH):** (a) chuỗi 4 bước chạy sống end-to-end, không stage gãy; (b) hiểu recall/precision từng trường bằng spot-check mắt (chưa gold); (c) đo A/B 1-call-gộp vs 2-call-tách để quyết kiến trúc bằng SỐ; (d) log token/cost thật.

## 2. Scope

**IN:** ingest WH (#768) → document.json block-aligned (ch1–4 tối thiểu, full 34 ch nếu rẻ); Builder 4 bước; schema output từng bước; consolidation+audit ghi artifact; A/B probe 1 chương; spot-check checklist.

**OUT (không lan man):**
- ❌ **KHÔNG lấy/scrape bản dịch Việt** (Dương Tường). Có bản quyền + KHÔNG cần cho Builder — chỉ dùng ở chấm ref-based L4, lấy bằng bản mua + OCR. Bước này chỉ input **tiếng Anh public-domain**.
- ❌ KHÔNG dựng gold/oracle WH (để sau). Pilot chấm bằng **spot-check mắt**, không cần denominator.
- ❌ KHÔNG chạy full 34 chương (chờ pilot hiệu chỉnh xong).
- ❌ KHÔNG ghi DB production. Pilot ghi artifact JSON riêng.
- ❌ KHÔNG Translator, KHÔNG RAG/vector-index (đó là L2b/L3 sau khi FREEZE).
- ❌ KHÔNG kết luận quan hệ/pha ở bước window (chỉ evidence).

**Local sources already pinned (2026-07-08):**
- EN EPUB: `reference/literary/wuthering_heights/en/wuthering_heights_gutenberg_768_epub3_images.epub`
  (Project Gutenberg #768, 34 chapters, SHA256 `3F8B0EF1F30026B979A8CFB2488603ED288E75EDDEDB4407919491C47B649B89`).
- VI candidate for later reference alignment only: `reference/literary/wuthering_heights/vi/doi_gio_hu_vi_full_34ch_candidate.epub`
  (user-supplied legal EPUB, 34 numbered chapters, Ch.1 spot-check no abridgement signal,
  SHA256 `6737B7DC333C7795A5BB6987274C78C27425DA8B842C89295AA62C2B5B4B84BE`,
  `translator_unverified=true`). **Do not feed VI into Builder.**

## 3. Ingest WH *(CodeX)*

- Nguồn: Project Gutenberg #768 (Emily Brontë, public domain). Chuẩn hoá về `document.json` block-aligned **giống TI** (units + blocks, block_id ổn định).
- **Cảnh báo unit-id** (bài học TI): WH có cấu trúc Volume I/II + 34 chương. Ghi **bảng mapping** `unit_id ↔ (volume, chương tự sự I–XXXIV)`; đừng để heading Volume giả làm chương. Xuất mapping ra file, commit.
- Pilot cắt **chương 1–4** (Lockwood đến → 2 lần thăm/đàn chó/lẫn tên → phòng ngủ ván sồi + nhật ký + ma + Heathcliff khóc → Nelly bắt đầu kể, Heathcliff bé xuất hiện). Đoạn này cố ý chọn vì: **kể lồng** (khung Lockwood + Nelly), **lẫn tên khủng khiếp** (hai Catherine, Earnshaw/Linton/Heathcliff/Hareton — stress test disambiguation nhân vật đỉnh nhất của tiếng Anh), và mở đầu quan hệ.

## 4. Builder 4 bước *(Claude — thiết kế)*

**Cơ chế vòng lặp:** Bước 1 & 2 chạy TRONG CÙNG một vòng lặp window (mỗi window bắn 2 call, KHÔNG đọc sách 2 lần). Bước 3 là vòng lặp theo chương. Bước 4 toàn cục. Chi phí đọc ≈ 2× window + 1× chương.

### Bước 1 — Lexicon window (cục bộ, kế thừa D2L)
Input: window vài block. Output ledger (mỗi entry kèm `block_id`):
- `proper_nouns`: tên riêng người/địa danh (Wuthering Heights, Thrushcross Grange, Gimmerton…) + surface.
- `terms`: từ mang tải văn hoá/thời đại cần nhất quán (nếu có).
- `name_mentions`: mỗi lần một cái tên/biệt danh/đại từ-định-danh xuất hiện — `surface` + `refers_to_hint` (gợi ý entity, **là bằng chứng KHÔNG phải kết luận**: ví dụ `surface="the master" → hint: Heathcliff? @block`).

### Bước 2 — Narrative evidence window (cùng vòng lặp b1)
Output ledger (evidence thuần, **cấm phán pha**):
- `speaker_turns`: `utterance_gist` · `speaker_surface` · `addressee_surface` · `block_id`.
- `relation_events`: `actor_surface` → `target_surface` · `event_type` (mở: addresses-as / strikes / protects / mocks / serves / …) · `quoted_address` (term xưng hô nguyên văn: "Nelly", "Mr. Heathcliff", "master") · `block_id`.
- `address_observations`: X gọi Y bằng "___" + register cue (formal/intimate/hostile) — quan sát cục bộ.
🔒 Guard (ghi NGUYÊN VĂN trong prompt): *"Report only what is observable in these blocks. Do NOT infer relationship type, alliance, or emotional phase — those are decided later from the full timeline. Every utterance with a clear speaker must appear once in speaker_turns with block ids."*

### Bước 3 — Chapter digest (trọn chương, STRUCTURED)
Input: **cả chương** + rolling summary chương trước (bounded ~200 tok). Output **structured, KHÔNG prose** (CodeX điểm 3), mỗi trường kèm `block_evidence`:
- `state_changes`: thay đổi trạng thái/hoàn cảnh nhân vật trong chương.
- `relation_events`: sự kiện quan hệ tầm chương (bắt được vòng cung mở-đầu-chương → kết-cuối-chương mà window bị chặn bỏ lỡ).
- `unresolved_threads`: mạch mở chưa giải (con trỏ tương lai để b4 biết chờ payoff chương sau).
- `motifs`: motif/hình ảnh lặp (cửa sổ, ma, đồng hoang…).
- `chapter_summary`: 3–5 câu gói, để nối rolling summary — nhưng KHÔNG thay structured trên.

### Bước 4 — Consolidation + audit (code cơ khí + LLM điểm huyệt)
Đọc mọi ledger b1–b3, sinh memory THẬT (artifact) = **Hồ sơ tác phẩm / Story Bible**:
- **Entity dedup** (kiểu D2L C2): gộp `name_mentions` thành entity + alias set. **LLM đề xuất merge, code quyết bảo thủ.**
  - 🐦 **CANARY WH (bắt buộc kiểm):** hai Catherine (Catherine Earnshaw mẹ vs Catherine Linton con) **KHÔNG được merge**; Hareton Earnshaw ≠ Hindley Earnshaw; Linton Heathcliff ≠ Heathcliff. Đây là hiện thân trực tiếp của `builder-over-merges-high-freq-head-nouns`. Merge sai cặp này = FAIL.
- **Timeline reconciliation** (RECONCILE §2.1): ghép `relation_events` thành khoảng pha — **no-overlap + trigger evidence + preserve time order** (KHÔNG majority vote). Pha chỉ apply sau human review (giữ nguyên `weighted-ledger-promotion-three-gate`: audit-label + keep-source + no-new-collision).
- **Term/Auditor precision** (kiểu D2L C3): Auditor tầng-2 lọc entry rác/generic, giữ sàn recall. Code cơ khí (dedup surface, sum occ), phán đoán = LLM.
- Output artifact JSON: `registry_T1_glossary`, `registry_T2_entities` (+mentions+alias), `registry_T3_speaker_turns`, `registry_T4_chapter_digests`, `entity_relations` (pha, có `valid_from/to_block`, `address_policy` theo pha). **Ghi file, KHÔNG DB.**

## 5. A/B probe kiến trúc call *(đo, không tin)*
Trên **1 chương dày thoại (ch2)**: chạy (A) 1 call gộp lexicon+narrative vs (B) 2 call tách (b1+b2). Đếm: `name_mention_recall`, `speaker_turn_recall`, `parse_fail`, `token_in/out`, `cost`. Mục tiêu: quyết tách hay gộp bằng SỐ (measure-before-trust). Giả thuyết Claude: tách thắng recall đủ bù ~2× input window; nếu KHÔNG, gộp lại.

## 6. Spot-check (chưa gold — CodeX tự soi, Claude verify độc lập)
Trên ch1–4:
1. **Entity:** canary hai-Catherine giữ tách? địa danh WH/Grange/Gimmerton bắt được? alias Heathcliff đúng?
2. **Speaker turns:** lấy mẫu 10 lượt → speaker/addressee đúng?
3. **Chapter digest ch3:** có bắt threads nhật ký/ma/Heathcliff-khóc + `unresolved_threads` không?
4. **Traceability:** mỗi `relation_event`/`state_change` có `block_evidence` mà block đó THẬT chứa sự kiện (không bịa block-id).
5. **Recall sanity:** đếm tay nhân vật có tên trong ch4, so ledger.

## 7. Deliverables & Acceptance

**L2A-0 (scaffold) acceptance — 3 fixture validate-only BẮT BUỘC (CodeX round-5), chạy TRƯỚC khi gọi API:**
1. **Group addressee KHÔNG bị mint thành person:** input speaker_turn có `addressee.reference_kind=group` ("the household") → consolidation KHÔNG tạo entity-person cho nó (validator §3.5 rule g); `resolution_status=named` cho group KHÔNG bị cờ.
2. **Vocative gắn đúng target:** input turn có `address_term_used="wife"` → addressee là **Mrs. Earnshaw** (person), KHÔNG phải "the household"; vocative-có-thì-addressee-là-người-cụ-thể (B2 prompt rule).
3. **Pilot Story Bible tự khai PARTIAL scope:** artifact có `scope=ch1–4`; interval open ghi `status="open_within_scope"` (hoặc đóng tới `artifact_scope_end_block`), **KHÔNG** close tới end-of-book (§5.6).

**Deliverables:** (1) `document.json` WH + bảng mapping unit↔chương; (2) artifact JSON 4 bước (ledger b1–b3 + Story Bible b4, có `scope`); (3) bảng A/B probe; (4) note spot-check CodeX tự soi + log token/cost.
**Acceptance L2A-1 (pilot PASS khi):** 4 bước chạy end-to-end ch1–4 không stage gãy; artifact đủ 4 bước; **canary set §1.7 PASS** (hai/ba Catherine không merge + valid_range, narrator Lockwood≠Nelly, "the master" theo range); `#phase_leak=0`; A/B có số ra quyết định; mọi evidence có block-id truy vết được; cost log đầy đủ. **Claude verify lại trên artifact thật trước khi tính PASS.**
**Non-goal reminder:** đây là pilot HIỂU-để-HIỆU-CHỈNH, KHÔNG phải số đẹp; sai/thiếu ở pilot là input để sửa prompt, chưa scale.
