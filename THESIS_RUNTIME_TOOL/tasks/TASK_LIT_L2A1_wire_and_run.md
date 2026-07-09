# TASK LIT-L2A-1 — Wire prompt B1–B4 + escalation vào runner, chạy pilot tăng dần (ch1 → ch1–4)

Status: SPEC READY (Claude, 2026-07-08). Tiếp sau **L2A-0 PASS** (Claude verify độc lập: validator thật qua probe-ngược, 232 block/49 window, WH EN #768, 9 test, 0-API, D2L prompt nguyên vẹn).
Type: WIRING + run đo-để-hiệu-chỉnh. Gọi API tier runtime. Ghi **artifact Story Bible (partial scope)**, KHÔNG DB. Chạy TĂNG DẦN, Claude gate giữa các bước.
Owner: **prompt = CLAUDE** (design doc, COPY VERBATIM — CodeX KHÔNG sửa wording; chỉ báo lại nếu run lộ lỗi prompt, không tự quyết). Wiring/runner/injection/escalation/run = CodeX. **Verify gate = Claude, KHÔNG uỷ quyền.**

- **NGUỒN CHUẨN:** `design/LITERARY_PROMPT_DESIGN.md` — B1 §2, B2 §3, B3 §4, B4 §5 (+L3b), nguyên tắc §1.5–1.9. Scaffold có sẵn: `pipeline/literary/builder_pilot.py` (validators đã verify thật), CLI `pipeline/scripts/run_literary_builder_pilot.py`.
- **Memory BẮT BUỘC:** `token-growth-halt-and-audit` (dừng+audit nếu token/call siêu tuyến), `real-usage-field-map` (đọc token/cost THẬT từ cache usage_json, KHÔNG cap), `one-button-cost-cap-semantics`, `prompt-memory-design-is-first-class`, `builder-v2-memory-pack-design`, `gemma-local-t3-locator-adopted` (confidence LLM ~vô nghĩa → gate theo method), `literary-memory-is-interval-valued-as-of-query`, `write-tool-unicode-path-phantom`.
- **Branch/Commit:** (điền khi imple)

## ⛔ M1 GATE RESULT (Claude verify độc lập trên artifact thật, 2026-07-08) — CHƯA qua, re-run M1
Pipeline chạy thật OK, trích ĐÚNG CHẤT (Thrushcross Grange/Wuthering Heights=place; Heathcliff/Lockwood/Joseph/Hareton=named; event toàn động-từ-cục-bộ; **phase_leak=0**; cost $0.03 ch1, không siêu tuyến). CodeX kỷ luật đúng (không tự sửa prompt). **3 lỗi PROMPT (Claude sở hữu) đã vá trong `design/LITERARY_PROMPT_DESIGN.md`:**
1. **[BUG] Prefix block-id:** ví dụ schema dùng `ch01_b` → model copy → 3/7 window fail (`ch01_b005` ∉ window `wh_ch01_b005`). Đã đổi 29 block-id ví dụ sang `wh_ch..` + thêm rule "COPY VERBATIM, không bỏ prefix" (B1/B2/B3).
2. **[Precision B1]** over-extract descriptor/đồ vật ("stalwart limbs", "the apartment") thành mention → thêm rule loại.
3. **[Precision B2]** event target phi-người (chó/phòng) → thêm rule actor/target chỉ người/narrator.

**Hành động CodeX:** (a) **re-sync 4 prompt từ design doc đã vá** (verbatim, không chế thêm); (b) **re-run M1 ch1**; kỳ vọng `lexicon_failed → 0`, retry giảm, mention không còn đồ vật, event không còn chó/phòng. (c) STOP, gửi lại → Claude verify M1 lần 2 trước M2. Ledger `Mr. Heathcliff`≠`Heathcliff` (2 entity) ở M1 là ĐÚNG (L1 consolidation ở M3 merge) — KHÔNG phải lỗi.

### M1 v2 (2026-07-08) — 3 lỗi cứng SẠCH, 2 lỗi mềm mới → vá + re-run v3
Claude verify v2: lexicon 7/0, narrative 7/0, phase_leak 0, **bad_prefix=0 mọi window**, event person-only (validator guard CodeX ok), descriptor-mention hết, recall thật giữ nguyên. **2 lỗi mềm mới (prompt, đã vá design doc):** (1) tên riêng rõ (`Joseph`/`Heathcliff`/`Hareton`) bị `unknown` thay vì `named` → làm nhiễu unknown-rate → rule named nói rõ "tên riêng luôn named"; (2) `poker/frying-pan/cellar/hearth` lọt glossary → siết bar object + thêm negative example.
**Hành động:** re-sync 2 chỗ đã vá → **re-run M1 (v3)** → gửi lại. **Stopping rule (Claude):** chỉ hard-fix lỗi phá KỶ LUẬT/METRIC (resolution_status, phase_leak, block-id); nhiễu cosmetic (1 object lọt) mà **B4 Auditor sẽ dọn** thì KHÔNG chặn M2 — đừng đánh bóng vô hạn raw ledger B1/B2.

### M1 v3 (2026-07-08) — proper-name & glossary SẠCH; còn 1 lỗi VALIDATOR-granularity → fix code, 0-API re-validate
Claude verify v3: proper-name đều `named` ✓; block-id sạch; phase_leak=0; glossary hết đồ bếp. **w007 fail KHÔNG phải lỗi prompt:** model ghi TRUNG THỰC `dogs/bottle/biter` = `reference_kind: unknown` (không nói dối "person"), nhưng **validator guard fail CẢ window** → nuốt luôn event tốt `he offers Mr. Lockwood`. **Fix (CodeX, code): non-person actor/target event → DROP entry đó + đếm `#nonperson_event_dropped`, GIỮ window** (design §1 "Validator granularity" mới). `cellar`/`knee-breeches`/`gaiters` trong glossary = **cosmetic, để Auditor M3 dọn, KHÔNG chặn M2, KHÔNG thêm vào negative-example.**
**Hành động:** sửa validator drop-not-fail → **re-validate raw v3 đã lưu (0-API)** để xác nhận w007 pass + `#nonperson_event_dropped` đếm đúng → gửi lại. Nếu narrative 7/0 và drop-rate hợp lý → **M1 PASS → M2**. KHÔNG cần gọi API mới (raw đã lưu).

### ✅ M1 PASS (Claude verify độc lập, 2026-07-08)
Re-validate 0-API: lexicon 7/0, narrative 7/0, phase_leak=0, block-id sạch, proper-name→named, `nonperson_event_dropped=3` (w007 giữ "he offers Mr. Lockwood", drop 3 chó/chai/cắn), raw nguyên vẹn, 14 focused test pass (Claude tự chạy), frozen hash nguyên. Prompt B1/B2 giờ hardened qua 3 run + 1 revalidate (tổng ~$0.10). **Mở M2.**
**M2 (CodeX):** wire B3 `literary_digest_v1` (design §4), chạy digest ch1 (trọn chương + rolling summary + ledger b1/b2 compact) → validate → STOP → Claude verify (narration_frame_segments bắt đúng narrator? unresolved_threads? relation_event_summary=evidence_only, KHÔNG finalize pha? translator_relevant_facts ≤8?). Chưa M3.

### ✅ M2 PASS (Claude verify độc lập, 2026-07-08)
Digest ch1 PASS: mọi `relation_event_summary.status=evidence_only`, KHÔNG `phase_label`/`valid_from_block`/`valid_to_block`, KHÔNG bịa `candidate_transition`; narration frame phủ `wh_ch01_b001`–`wh_ch01_b028` với `ent_mr_lockwood`; `translator_relevant_facts=6 ≤ 8`; block-id verbatim. Attempt 1 bị validator bắt thiếu `b001`, retry tự vá → validator hoạt động thật.

**M3 watch-items BẮT BUỘC verify trên Story Bible ch1:**
1. `ent_heathcliff` + `ent_mr_heathcliff` phải merge thành MỘT entity với alias valid_range (`Heathcliff`, `Mr. Heathcliff`).
2. `ent_hareton_earnshaw` từ inscription b012 phải là `mentioned_historical`, KHÔNG thành actor/speaker/relation participant hiện diện.
3. `character_state_changes` tạm của Lockwood (`residence: visiting_wuthering_heights`) phải bị Auditor cắt, KHÔNG thành state interval bền.

**M3 (CodeX):** wire B4 consolidate ch1 → Story Bible partial → validate `validate_story_bible` + canary/watch-items trên → STOP. Chưa M4.

### ⛔ M3 GATE RESULT (Claude verify độc lập, 2026-07-08) — CORE PASS, 1 BLOCKER FIX trước M4
Core consolidation PASS: identity merge `ent_heathcliff`+`ent_mr_heathcliff`, Hareton=`mentioned_historical`, Lockwood temporary residence dropped, open intervals, scope partial, canary pass. **BLOCKER:** `_propose_address_policies` hardcode tiếng Việt (`self="tôi"`, `address="ông"`, branch `target_id == ent_joseph`) → code làm việc ngôn ngữ + không generalize.

**Fix bắt buộc:** address policy code chỉ được emit bằng chứng cơ học: `observed_terms` (vocative EN), `evidence_level` observed/unsupported, `pair`, `phase_ref`, review/runtime flags. `self`/`address`/`register` để rỗng cho cả observed lẫn unsupported; xưng-hô VN defer sang LLM micro-call chuyên dụng hoặc Translator runtime. Re-run M3 0-API → STOP trước M4.

## 0. Vá 2 gap L2A-0 (Claude phát hiện khi verify)
1. **Context-only tail:** thêm `PREVIOUS/NEXT_WINDOW_TAIL` 1–2 block dạng CONTEXT_ONLY vào window (design §1.5/§2.2). Render tách khỏi window chính; validator SẴN chặn block_id ngoài window chính (không đổi). Không có tail → metric `#context_only_used_true` vô nghĩa.
2. **Tên mode:** thống nhất `literary_narrative_v1` (design §3) — sửa `literary_narrative_evidence_v1` trong manifest cho khớp.
(Cân nhắc, KHÔNG bắt buộc pilot: cắt window theo ranh giới scene/thoại "when possible"; nếu chưa làm, giữ fixed 8-block + tail.)

## 1. Wire prompt B1–B4 (VERBATIM từ design doc)
Đưa 4 prompt vào `pipeline/prepass/prompt.py` (hoặc module literary riêng) làm mode mới, **copy nguyên văn system prompt + user template** từ design doc; KHÔNG diễn giải lại. Wiring gồm:
- **Vòng window (B1+B2 cùng loop):** mỗi window → call `literary_lexicon_v1` rồi `literary_narrative_v1`. Injection builder (code):
  - B1: `REGISTRY_CONTEXT_PACK` = entity/glossary registry-so-far CÓ surface trong window (bounded ≤~15 dòng/~300 tok, KHÔNG full-dump).
  - B2: `ACTIVE_NARRATOR_HINTS_BY_BLOCK_RANGE` (heuristic said-tag/"I", hoặc unknown) + `CHAPTER_ROSTER_ON_STAGE` (tích luỹ trong chương từ mention b1) + `WINDOW_MENTIONS_FROM_LEXICON_PASS` (mention b1 của window này).
- **Vòng chương (B3):** call `literary_digest_v1` với TRỌN chương + `PREVIOUS_CHAPTER_ROLLING_SUMMARY` + `CHAPTER_ROSTER` + `CHAPTER_RELATION_EVENTS` (compact từ b2).
- Parse mọi output qua validator SẴN CÓ; entry lỗi → log + loại theo report (không tự sửa ngôn ngữ).
- **Model:** tier runtime (gpt-5.4-mini pin như D2L Builder/one-button). Đọc token/cost THẬT từ cache `usage_json` per-stage (`real-usage-field-map`), KHÔNG cap.

## 2. Wire B4 consolidation pipeline (4 lớp + L3b, design §5)
Pipeline KHÔNG một call. Code làm nặng, micro-call LLM CHỈ cho ca mơ hồ (kèm lát cắt evidence):
- **L1 identity:** cluster code exact-name → micro-call `literary_identity_adjudicate_v1` cho cluster mơ hồ (ưu tiên SPLIT) → gán alias `valid_from/to_block` (open) → **canary §1.7**.
- **L2 interaction:** resolve speaker/addressee bằng turn-alternation+roster+`attribution_method` (gate theo METHOD, KHÔNG confidence); unknown quan trọng → **escalation queue** (scene-slice, output chỉ sửa resolution, cấm thêm fact).
- **L3 relation-phase:** micro-call `literary_phase_segment_v1` per cặp (taxonomy 8 nhãn, open-interval, trigger-evidence, "ít pha khi yếu"); code validate non-overlap/order; cặp ≥2 candidate_transition/flip mạnh mà reject → `blocked_for_runtime` (KHÔNG single-phase che under-seg). `valence` code suy từ phase_label, KHÔNG LLM.
- **L3b character-state:** code gom `character_state_changes`→`entity_state_intervals` (open, enum attribute); escalate CHỈ khi mâu thuẫn.
- **L4 address-policy:** micro-call `literary_address_policy_v1` per (pair×phase), MỖI CHIỀU object riêng + `evidence_level` (observed/inferred/unsupported) + `needs_human_review`; chiều `unsupported` KHÔNG dùng runtime; **proposal-only**.
- **Auditor precision** (drop rác, giữ sàn recall) → **canary gate** (fail LỚN) → **freeze PARTIAL** (`scope=ch1–4`, `status=open_within_scope`, KHÔNG đóng end-of-book) → artifact **Story Bible**.

## 3. Cost & token (gate an toàn)
- Log per-stage: input/output token + cost THẬT (usage_json) + model + #calls (window×2 + chương + micro-calls B4).
- **HALT + audit** nếu token/call siêu tuyến hoặc tổng vượt kỳ vọng (`token-growth-halt-and-audit`); prompt log trong cache để soi.
- Cost pilot dự kiến < vài $ (mini, ch1–4) — nhưng ĐO thật, không đoán.

## 4. A/B probe (đo, không tin)
Trên **ch2** (dày thoại): (A) 1-call gộp lexicon+narrative vs (B) 2-call tách. Đếm `name_mention_recall`, `speaker_turn_recall`, `#parse_fail`, token, cost. Ra quyết định tách/gộp bằng SỐ (measure-before-trust). Nếu schema-giàu (6 field/participant) làm rớt recall rõ → báo Claude cân nhắc cắt field.

## 5. Thứ tự chạy — TĂNG DẦN, Claude gate giữa mỗi mốc (KHÔNG một mạch)
- **M1:** wire B1+B2, chạy **CHỈ ch1** → validate + log cost → **Claude verify ledger bằng mắt** (mention/thoại đúng? unknown-rate 2 phía? phase_leak=0?). Gate.
- **M2:** wire B3, chạy ch1 → validate → Claude verify (narration_frame_segments bắt chuyển narrator? threads?). Gate.
- **M3:** wire B4, consolidate ch1 → Story Bible partial → **canary check** → Claude verify (alias valid_range? blocked_for_runtime đúng? address unsupported?). Gate.
- **M4:** ch1 sạch + cost ổn → scale **ch1–4** + A/B probe ch2. Claude verify toàn bộ.
- KHÔNG nhảy mốc khi mốc trước chưa Claude-gate.

## 6. Deliverables & Acceptance
**Deliverables:** (1) prompt B1–B4 wired (verbatim), diff nhỏ; (2) Story Bible artifact ch1(M3) + ch1–4(M4) có `scope`; (3) per-stage cost/token log thật; (4) bảng A/B; (5) canary report; (6) note unknown-rate 2 phía + escalation stats.
**Acceptance (Claude verify trên artifact THẬT trước khi PASS):** wiring == design (prompt verbatim, injection đúng); `#phase_leak=0`; **canary §1.7 PASS**; open-interval dùng `open_within_scope`; address unsupported KHÔNG vào runtime-usable; cost log thật + không siêu tuyến; A/B ra quyết định. Prompt do Claude sở hữu — CodeX sửa wording chỉ khi báo lại + Claude đồng ý.
**Non-goal:** KHÔNG số đẹp; KHÔNG Translator/RAG/as_of (đó là L2b); KHÔNG bản dịch VN; KHÔNG DB; KHÔNG scale >4 chương ở pilot.
### M3 rework v2 (CodeX, 2026-07-08) -- name-free consolidation
Claude's second M3 gate found that consolidation still had book/ch1-specific answer tables
(`surface -> entity`, `pair -> dependent`, and fixed canonical ids). Those tables are removed.

Current M3 consolidation now:
- builds entity ids/canonical names from the M1 entity ledger with a generic honorific-strip rule;
- resolves references from `candidate_entity_ids` first, then from the ledger-derived alias index;
- maps active narrator from B3 `narration_frame_segments`;
- leaves unresolved references unresolved instead of guessing from pronouns/vocatives;
- computes historical-vs-runtime presence from actual resolved roles, not a named canary branch;
- drops temporary residence state by generic `attribute=residence + visiting + observed_scope=this_chapter`;
- keeps address policy evidence-only (`observed_terms`/`evidence_level`, blank self/address/register);
- marks relation phase as `phase_source=observed_valence_hint_fallback` + `needs_human_review` until the real L3 micro-call is wired.

Re-run M3 remains 0-API and `needs_claude_gate`; this is intentionally not a proof that
phase segmentation works yet.
