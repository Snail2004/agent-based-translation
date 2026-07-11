# LITERARY_PROMPT_DESIGN — prompt + schema Builder văn học 4 bước (Story Bible)

Owner: **Claude viết prompt + schema** (lõi luận văn). CodeX wiring/parser/validator, chỉ chỉnh prompt khi run lỗi nghi-prompt + báo lại. Xem TASK_LIT_L2A §0, memory `codex-division-of-labor-and-relay`, `prompt-memory-design-is-first-class`.
Corpus pilot: Wuthering Heights (Gutenberg #768), chương 1–4. Trạng thái: **L2A-1 M1 chạy thật (ch1); prompt vá sau M1 gate — block-id verbatim (wh_ prefix) + precision mention/event**. Block-id trong ví dụ = scheme thật `wh_chNN_bNNN`.

## 0. Bối cảnh: cái đã có vs cái ta làm

`pipeline/prepass/prompt.py` đã có `build_messages(mode="literary")` = **`literary_builder_context_v3`**: MỘT call/chương, trả cả glossary + entities + relations (kèm address_a_to_b_vi/state_label) + summary + motifs. CodeX preflight ra **`address_policies=0`** → cục monolith đuối phần quan hệ/xưng hô (ép model kết luận quan hệ trong một lượt = đúng bẫy D2L first-write-wins).

Redesign 4 bước = tách theo "đơn vị sự thật": lexicon/mention cục bộ → evidence tự sự cục bộ → digest trọn chương → consolidation toàn cục. **Cục `literary_builder_context_v3` giữ nguyên làm baseline A/B** (§5 task) — KHÔNG xoá.

## 1. Quy ước chung (mọi bước)

- **House style** (khớp `build_messages`): `system` = "You are the <ROLE> agent for an autonomous English-Vietnamese literary translation pipeline. Read only … Hard rules: … Required JSON shape: {…}". `user` = các khối có nhãn IN HOA. Block marker render qua `render_chapter_blocks` → mỗi dòng `[wh_ch01_b005] <text>`.
- **JSON-only**, không văn xuôi ngoài JSON. VI target **đủ dấu** (cấm ASCII kiểu "doi gio hu").
- **block_ids luôn là marker THẬT nhìn thấy trong khối text được đưa** — validator loại entry có block_id không xuất hiện trong window (chặn hallucinate).
- **Evidence-ledger**: bước window (1,2) CẤM kết luận danh tính cuối / quan hệ / pha. Chỉ ghi cái quan sát được + hint. Danh tính & pha do Bước 4 quyết.
- **KHÔNG full-dump registry**: pack context chỉ gồm entry có surface xuất hiện trong window, có trần token (`builder-v2-memory-pack-design`).
- **Validator granularity (DROP entry, KHÔNG fail window):** lỗi cấp-ENTRY (block-id sai, non-person actor/target, enum sai, đại từ trần) → **bỏ RIÊNG entry đó + đếm** (`#nonperson_event_dropped`, `#dropped_bad_block`…), **GIỮ các entry tốt còn lại**; window chỉ FAIL khi JSON parse-fail hoặc thiếu top-level. Lý do: 1 event chó không được nuốt luôn event người-người tốt trong cùng window (bài học D2L "drop bad entry, keep good"). **Drop-rate cao = cảnh báo prompt** (đừng nuốt im lặng — vẫn phải surface để bắt lỗi hệ thống như bug block-id).

## 1.5 Danh tính xuyên request & quy tắc window *(chốt với user 2026-07-08)*

**Nguyên tắc nền: Builder KHÔNG phân giải đại từ per-occurrence.** Story Bible lưu tri thức cấp-cuốn-sách (entity+alias, speaker map, relation phase, address/narrator-reference policy). "he/I ở block X là ai" = việc Translator xử lúc dịch window đó bằng pack (roster + policy) — như người dịch thật, không có bảng tra đại từ toàn sách. Vì vậy B1 cấm đại từ trần làm mention surface: đại từ trôi qua là ĐÚNG thiết kế.

**Thang nối danh tính (rẻ → đắt, kiểu cascade D2L localize):**
1. Window ghi bằng chứng, `unknown` được phép (đã cài prompt B1/B2).
2. Nhồi `CHAPTER_ROSTER_ON_STAGE` bounded (chương hiện tại, không cả sách).
3. B4 code rẻ: nối said-tag, thoại xen kẽ A-B-A-B, khớp alias.
4. **Escalation queue:** ca unknown còn lại → re-query LLM với lát cắt rộng hơn (scene/chương) CHỈ cho ca đó. Đo: `#unknown_resolved_by_code` / `#resolved_by_llm_escalation` / `#unresolved`. KHÔNG nguyên-chương cho mọi call — input dài làm rớt recall (bài học D2L), chương-nguyên-cục chỉ cho B3 + escalation.

**Quy tắc windowing (CodeX imple ở scaffold):**
- Cắt window theo ranh giới scene/đoạn thoại khi có thể (không chém ngang cuộc đối thoại).
- Kèm `PREVIOUS_WINDOW_TAIL` 1–2 block dạng CONTEXT_ONLY (chỉ đọc cho liền mạch; **cấm trích entry từ đó** — validator sẵn chặn block_id ngoài window chính).

**Narrator frame (đặc thù WH, bắt buộc):** "I" của Lockwood ≠ "I" của Nelly. Story Bible track `narration_frame_segments` = list `{block_range, narrator}` — **theo BLOCK-RANGE, không phải 1 narrator/chương** (ch4 chuyển Lockwood→Nelly GIỮA chương; narrator-theo-chương sẽ gán sai "I" ở đoạn chuyển). Digest B3 xuất segments này; narrator-reference policy (Nelly gọi Heathcliff = "hắn/ông"?) neo theo frame. Time-skip WH không phát sinh việc mới: pha quan hệ neo theo block-range THỨ TỰ VĂN BẢN (thứ tự Translator dịch), không dựng niên biểu thật.

**Mark & chấm (điều chỉnh scope so D2L):** markable bằng code = tên riêng + địa danh (glossary, surface-match kiểu TC giữ nguyên) + address_term trong thoại (ACS trên speaker_turns). KHÔNG mark bằng code = đại từ trần (policy-driven, judge/spot-check), sự kiện/motif (memory-only, không chấm marking). Địa danh CÓ yêu cầu nhất quán 1:1 qua glossary.

## 1.6 Tinh chỉnh sau review CodeX *(2026-07-08)*

**Resolution là FIELD hạng nhất (không nhị phân).** Mọi ô tham chiếu người (mention/speaker/addressee/actor/target) mang: `resolution_status` ∈ {named, candidate, unknown} (named = surface tự nó là tên/biệt danh rõ, B4 mint/gắn entity; candidate = mô tả/đại từ ánh xạ tới entity trong pack/roster nhưng chưa chắc; unknown = không attribute được) + `candidate_entity_ids` (số nhiều, chỉ điền khi candidate, lấy từ pack/roster — cho phép "the master" ứng viên nhiều người) + `mention_id`/`turn_id`/`event_id` ổn định (`m_<block>_<n>`) để B4+escalation trỏ ca cụ thể. Window pass CẤM mint canonical entity_id; `canonical_entity_id` để null (B4 quyết).

**Đại từ-làm-attribution KHÔNG đẻ rổ riêng** (không `coreference_evidence` bucket — thêm rổ = rớt recall). Đại từ nắm qua `attribution_method` trên turn; pronoun-as-actor cho phép làm surface với resolution_status candidate/unknown, KHÔNG xoá về unknown khi có tín hiệu turn-order.

**⭐ Alias có VÒNG ĐỜI (CodeX, đặc thù WH):** tên tái chế — Catherine mẹ Earnshaw→Linton; con Linton→Heathcliff→Earnshaw; "Catherine Linton" là alias CỦA CẢ HAI ở hai khoảng khác nhau. Alias trong T2 BẮT BUỘC có `valid_from_block`/`valid_to_block` gắn entity — không alias global vĩnh viễn. B4 gán; canary hai-Catherine kiểm cả valid_range.

**`confidence` = COARSE {high, medium, low}, KHÔNG numeric** (self-confidence LLM vô nghĩa — đã đo ở D2L, xem `reasoning-effort`/gemma). Tín hiệu tin cậy THẬT = `attribution_method` (explicit_tag > turn_alternation > nearby_context > narrator_inference) + "candidate có trong roster?". B4 gate theo METHOD, không theo confidence.

**Hai ngữ cảnh injection KHÁC NHAU (sửa CodeX):** Builder-time (B1/B2) CHỈ có `narrator hints theo block-range` (B2, refine thành `narration_frame_segments` ở B3) + `scene roster` + `previous summary`; relations/address-policy CHƯA tồn tại (là output B4). Translator-time (runtime, sau FREEZE) mới có pack đầy đủ relation facts + pair address policy theo pha. Đừng nhồi thứ chưa build vào Builder.

**`story_time` chỉ nhãn THÔ cấp chương** (`narration_frame` ở B3: "khung Lockwood 1801" / "Nelly hồi tưởng") — KHÔNG tái dựng niên biểu; pha neo theo THỨ TỰ VĂN BẢN.

**Unknown-rate là metric HEADLINE** (có ngưỡng cảnh báo), chia theo vai + `attribution_method`: unknown cao bất thường = prompt/windowing chưa ổn → KHÔNG scale.

## 1.7 Canary set, định nghĩa unknown, escalation *(CodeX round-2)*

**Bộ canary (B4 phải pass; spot-check kiểm):**
- Ba Catherine KHÔNG merge; mỗi alias có `valid_range` (Earnshaw / Linton / Heathcliff).
- "Catherine Linton" gán đúng **mẹ** (sau cưới Edgar) vs **con** (khai sinh) theo block-range.
- Heathcliff ≠ Mr. Earnshaw dù cùng vai "master" ở hai thời điểm; "the master" resolve theo block-range, KHÔNG global.
- narrator "I" khung Lockwood ≠ "I" khung Nelly (không cùng entity narrator).
- Hindley Earnshaw ≠ Hareton Earnshaw; Linton Heathcliff ≠ Heathcliff.

**unknown = có kỷ luật, KHÔNG phải thất bại:** báo tách `unknown_with_evidence` (đủ evidence để escalation xử) vs `unknown_empty` (mất bằng chứng — mới là xấu). unknown QUÁ THẤP cũng đáng nghi = đoán bừa → ngưỡng cảnh báo **2 phía**.

**Escalation queue (khoá tiêu chí cho B4):** CHỈ escalate unknown/candidate ảnh hưởng relation/address/speaker; bỏ qua unknown trivial; input = **scene slice** (không full-chapter mặc định); output CHỈ sửa resolution, **cấm thêm fact ngoài evidence range**.

## 1.8 Temporal state / vòng đời — khác biệt LÕI so D2L *(chốt với user 2026-07-08)*

**Nguyên tắc:** D2L memory = MAP tĩnh (term→dịch). Literary memory = tập **FACT CÓ KHOẢNG HIỆU LỰC**: `(subject, attribute, value, valid_from_block, valid_to_block, trigger_block, evidence)`. Truy vấn LUÔN là **"as-of block X"** (trạng thái TẠI vị trí đang dịch, KHÔNG phải state cuối/global). Đây là khác biệt cấu trúc lớn nhất & lõi kỹ thuật luận văn.

**Trường CÓ vòng đời (không chỉ relation):** relationship state cặp (bạn→thù→hòa→tri kỉ; KHÔNG đơn điệu, nhãn CÓ THỂ lặp) · address/xưng hô cặp (dẫn xuất) · alias/tên (married name, tước hiệu) · entity status (giai cấp, tuổi→register, **sống/chết**, nơi ở) · narrator-reference tone + narration_frame. **Tĩnh (global):** identity core, glossary địa danh/vật, motif-as-concept.

**BUILD (evidence → timeline, KHÔNG lưu 1 state):**
- B2 ghi `relation_events` (act cục bộ + block_id), CẤM nhãn pha.
- B4 lớp relation-phase = **segment chuỗi event theo THỨ TỰ thành interval**: ranh giới = trigger lật valence (evidence rõ); giữa 2 ranh giới, valence trội định state. Bài toán **change-point trên stream có thứ tự**, KHÔNG phải phân loại cả cặp.
- Builder KHÔNG giữ "current relationship" chạy dọc extraction (→ first-write-wins). Pha nảy ở B4 từ TOÀN BỘ stream; nhãn lặp (`friend[b10-40]` … `friend[b200-300]`) = 2 phase hợp lệ.

**Granularity = block = đơn vị dịch:** lật giữa đoạn biểu diễn được (`phase1[b100-147]`, `phase2[b148-…]`, `trigger=b148`; register đổi từ trigger trở đi). memory-resolution = translation-unit = block, không cần mịn hơn.

**DÙNG (as-of query — bất biến BẮT BUỘC):**
- Translator dịch `[X..Y]` → pack đưa, mỗi cặp có mặt, **phase AS-OF X** (address+register), KHÔNG state global. (Failure mode: lưu 1 state global → dịch đoạn đầu bằng giọng của quan hệ cuối truyện.)
- Trigger TRONG window (Z∈[X..Y]) → pack cờ `transition@Z: address P→Q` để Translator đổi giọng GIỮA window.
- Critic/ACS chấm nhất quán **TRONG-pha**; đổi giọng đúng ranh giới = ĐÚNG, KHÔNG đổi tại transition thật = LỖI → ACS phải phase-segmented.

**Coupling:** một trigger (Heathcliff giàu về) lật đồng thời relationship+address+alias+status. Mỗi trường = interval-list ĐỘC LẬP; B4 ghi chung `trigger_block` khi co-triggered (không gộp một structure = tránh monolith).

**ĐO (không tune mù):** under-segment (bỏ lật thật → giọng sai) vs over-segment (giọng chập chờn) — cần trigger-evidence gate + ngưỡng tối thiểu; cho phép lật NHANH nếu evidence mạnh (án mạng/hôn nhân). Số phase/cặp khớp người đọc = thứ phải đo. **Pilot ch1–4** kiểm CƠ CHẾ (thu event→segment pha đầu→as-of), chu kỳ đầy đủ để scale sau; `unresolved_threads` (B3) = con trỏ transition đang chờ.

## 1.9 Temporal model — siết CodeX round-3 *(2026-07-08)*

**Tier field temporal (pilot KHÔNG build hết — chống scope explosion):**
- **CORE (build L2A, register-critical):** `relation_phase`, `address_policy`, `alias/title`, `social_status`.
- **DEFERRED (schema để cửa, build sau khi core chạy):** `speech_style` (dialect Joseph, giọng trẻ con — quan trọng WH nhưng pha sau), `life_status` (chết/mất tích/bóng ma), `narrator_stance`, `knowledge_state` (ảnh hưởng register ÍT → ưu tiên thấp), `residence`. `allegiance` = **gộp vào `relation_phase`**, không field riêng.
- B4 phải BIẾT trường nào temporal (để không lưu global sai) nhưng chỉ build CORE ở pilot.

**Open interval (CodeX):** mới thấy trigger → `valid_from_block=b148, valid_to_block=null, status=open`; trigger kế mới đóng interval cũ. CẤM ép model đoán `valid_to_block` sớm → hallucinate. Freeze cuối sách đóng các interval open còn lại tới hết.

**Event ⟂ State (ranh giới cứng):** `relation_events` (B2, bằng chứng rời rạc) KHÔNG BAO GIỜ tới Translator. Chỉ `state_intervals` (B4, đủ evidence) vào pack. "Hindley chế Heathcliff @b14" = event; "Hindley hostile→Heathcliff [b14..open]" = interval.

**Hai trục thời gian (tách sạch):** `text_order_range` = TRỤC CHÍNH cho Translator (relation_phase index theo trục này) · `narration_frame`+`story_time_label` (thô) = ai kể + khi nào. Vì Nelly kể (đa phần) THEO THỨ TỰ → text-order ≈ story-order → as-of-block trả đúng pha của **cảnh đang kể** (bạn-bè-thời-trẻ trong hồi tưởng), KHÔNG phải trạng thái cuối. `story_time_label` chỉ CỜ khi kể phi tuyến (flashback lồng/foreshadow) — KHÔNG dựng chronology. Phân vai: relation_phase = chuyện trong cảnh (text-order); narration_frame = ai kể/thì.

**Bounded phase taxonomy (chống over-seg):** `phase_label` ∈ tập ĐÓNG nhỏ, phổ quát mọi tiểu thuyết (đề xuất: allied, friendly, neutral, strained, hostile, estranged, dependent, reconciled). LLM CHỌN từ tập + kèm trigger-evidence; code KHÔNG suy pha. (Không vi phạm `code-never-does-language-work`: enum trạng thái phổ quát ≠ wordlist trích thuật ngữ.) Mở phase mới CHỈ khi trigger có evidence ngôn ngữ/hành động MẠNH; tone nhất thời → ghi `event`, KHÔNG mở phase.

**Sub-block door (optional, CHƯA build):** mặc định interval theo block; trigger giữa 1 block dài/thoại quan trọng → cho phép `quote_trigger`/`span_hint` + cờ `transition_inside_block`. Schema chừa chỗ, pilot không làm.

**Query `as_of` = HỢP ĐỒNG RUNTIME (Translator, test ở L2b — KHÔNG phải Builder pilot L2A):** `story_bible.as_of(block_id, window_range)` → {active_narrator, scene_roster, valid_aliases@block, relation_phases cặp-có-mặt, address_policies cặp-có-mặt, transitions_in_window, uncertainty_flags}. CẤM trả full timeline / quan hệ tương lai / trạng thái cuối = **guard chống leakage NỘI BỘ trong Story Bible** (song song directional-lock gold-isolation).

---

## 2. BƯỚC 0 — Chapter pre-read  *(mode: `literary_chapter_brief_v1`)*

### 2.1 Nhiệm vụ
Đọc nhanh TRỌN một chương trước các pass chi tiết để tạo frame trung lập: ai đang có mặt, ở đâu, scene có mấy người có thể được gọi, và chuyện gì xảy ra ở mức factual. Đây là tầng **GIST**, không phải fact store và không được kết luận quan hệ/pha.

### 2.2 System prompt (nguyên văn)

> - Prompt version: literary_chapter_brief_v1.
> - You are doing a FAST PRE-READ of ONE chapter BEFORE any detailed extraction. Return only valid JSON matching the Required JSON shape. No text outside JSON.
> - Purpose: give the later passes a factual frame of WHO is on stage, WHERE, and roughly WHAT happens, so they can resolve pronouns and vocatives. You are NOT extracting evidence and NOT judging relationships.
> - HARD LIMIT (leak guard): do NOT output relationship states, alliances, feelings, trust, phases, or emotional outcomes; do NOT say two characters "become" anything. Report only observable roles and neutral actions. Relationship conclusions are decided far later from the whole timeline — asserting them here would corrupt that.
> - cast_on_stage: only persons who are physically present and act or speak in THIS chapter. Exclude persons merely named in passing, and exclude historical / portrait / inscribed / remembered names of people not present. For each: surface (verbatim, as first named), surface_kind (proper_name or descriptor), role_hint (a plain OBSERVED social role only — e.g. host, visitor, servant, child, innkeeper, traveller, soldier; NEVER a relationship word like friend, enemy, rival, lover), first_seen_block. Use surface_kind=proper_name only when the surface itself is a real name/title+name ("Mira", "Mr. Alden", "Miss Rook"); use descriptor for role/description surfaces ("his aunt", "the innkeeper", "the coachman").
> - First-person narrator rule: if the narrator is first shown as "I" but is later addressed or named in this same chapter, use that later proper name as the cast surface and mark surface_kind=proper_name; do not keep a separate "I" cast row for the same person. If the narrator is never named in this chapter, use surface "I" with surface_kind=descriptor.
> - setting: {place (verbatim if named, else a short description), time_frame_hint (one of: frame_present, past_recollection, unclear), scene_shape (one of: single_scene_one_location, few_scenes, many_scenes_or_travel)}.
> - scenes_party_size: split the chapter into contiguous scenes; for each {block_range, co_present_count (how many named persons are together and could be addressed in that scene), participants (their surfaces)}. This is the signal the detailed pass uses to tell whether a bare vocative has exactly one possible addressee.
> - neutral_premise: <=40 words, what happens at a plain factual level (who goes where, who meets whom, what is done). No inner-relationship verdicts, no spoilers of emotional arcs.
> - Prefer an entity_id (ent_*) from REGISTRY_SO_FAR when a name clearly matches; else use the clean surface.
> - Every block_id MUST be a marker that literally appears in this chapter's text.
>
> Required JSON shape:
> {
>   "chapter_id": "...",
>   "cast_on_stage": [
>     {"surface": "the innkeeper", "surface_kind": "descriptor", "role_hint": "innkeeper", "first_seen_block": "bk_ch01_b004"},
>     {"surface": "Ravel", "surface_kind": "proper_name", "role_hint": "traveller", "first_seen_block": "bk_ch01_b002"}
>   ],
>   "setting": {"place": "a roadside inn at dusk", "time_frame_hint": "frame_present", "scene_shape": "single_scene_one_location"},
>   "scenes_party_size": [
>     {"block_range": ["bk_ch01_b002", "bk_ch01_b016"], "co_present_count": 2, "participants": ["Ravel", "the innkeeper"]}
>   ],
>   "neutral_premise": "A tired traveller reaches an inn at nightfall, is questioned by the innkeeper about his business, and takes a room for the night."
> }

### 2.3 User template
```
REGISTRY_SO_FAR
{registry_lines_or_"(none yet)"}

NEIGHBOR_SUMMARIES_GIST_ONLY
{up_to_2_previous_chapter_summaries_or_"(none)"}

CHAPTER_ID
{chapter_id}

FULL_CHAPTER_TEXT_WITH_BLOCK_MARKERS
{render_chapter_blocks(chapter)}
```

---

## 3. BƯỚC 1 — Lexicon window  *(mode: `literary_lexicon_v1`)*

### 2.1 Nhiệm vụ
Trên MỘT window vài block, trích **hai loại "được gọi tên"**:
- **glossary_candidates** — vật/nơi/khái niệm văn hoá cần nhất quán cả sách (KHÔNG phải người).
- **character_mentions** — mỗi lần một tên/biệt danh/mô tả-xác-định của NGƯỜI xuất hiện (bằng chứng alias; danh tính gộp ở Bước 4).

KHÔNG làm: quan hệ, thoại, tóm tắt, motif (thuộc bước 2/3). KHÔNG mint entity mới / KHÔNG gộp danh tính.

### 2.2 Logic nhồi window-context
- **Text:** window = các block liên tiếp trong CÙNG một chương, **cắt theo ranh giới scene/đoạn thoại khi được**, tổng ≤ ~500 token nguồn (hoặc ≤ 8 block). Kèm `PREVIOUS/NEXT_WINDOW_TAIL` 1–2 block dạng CONTEXT_ONLY để đọc liền mạch — **cấm trích entry từ đó** (validator chặn block_id ngoài window chính). Render kèm marker.
- **Pack (`REGISTRY_CONTEXT_PACK`):** code quét surface trong window, tra Story-Bible-so-far, bơm **chỉ** entity/glossary có alias/surface xuất hiện trong window — mỗi cái 1 dòng `entity_id | canonical | 1-2 alias`. Trần ≤ ~15 dòng / ~300 token; vượt thì ưu tiên theo tần suất + mới gặp. Mục đích: tên quay lại được **nối vào entity_id cũ**, nơi cũ không bị đề xuất lại. Pilot ch1–4 registry nhỏ nên rẻ.
- KHÔNG bơm: quan hệ, digest, pha.

### 2.3 System prompt (nguyên văn, drop vào `prompt.py`)

> You are the Lexicon extractor for an autonomous English-Vietnamese literary translation pipeline. Read only the English source WINDOW provided by the user. Record two things that are *named* in this window: (1) glossary candidates — places, objects, and culturally specific terms that must stay consistent across the whole book; (2) character mentions — every surface that names or points to a PERSON. This is an evidence pass: you record what is visible, you do NOT decide identities or relationships.
>
> Hard rules:
> - Prompt version: literary_lexicon_v1.
> - Return only valid JSON matching the Required JSON shape. No text outside JSON.
> - The user gives you one WINDOW, not a whole chapter. Extract every qualifying item visible in this window; do not impose a count cap. Windowing keeps each call small; downstream code consolidates duplicates.
> - glossary_candidates are NOT people. Put places (a named house or estate, a town or village, a region — use the work's own proper names) and objects/terms that are NAMED, culturally/period-specific, or plot-significant. An object qualifies ONLY on that bar — NOT ordinary household implements or rooms. Negative examples: window, gate, chair, dinner, letter, moor (unless a proper place name), servant, poker, frying-pan, cellar, hearth, kitchen, table, fire.
> - Each glossary target proposed_target_vi MUST use full Vietnamese diacritics.
> - category MUST be exactly one of: place, object, cultural, other.
> - character_mentions: record every NON-PRONOMINAL surface that names or refers to a person — proper names, nicknames, shortened names, spelling variants, titles used as identity ("the master", "the mistress"), and characterizing epithets ("the ruffian"). One row per surface occurrence-group in this window. A bare pronoun (he/she/I/they…) is NOT a mention surface here even though it refers to a person — skip it entirely; it is resolved later in the narrative pass.
> - Do NOT put plain pronouns as a mention surface: i, me, my, you, your, he, him, his, she, her, we, us, they, them, and their possessives.
> - character_mentions are SINGLE persons. Do NOT record a group ("the household", "the Lintons", "the servants") here — groups appear only in the narrative pass as an addressee/target with reference_kind group.
> - A character_mention must REFER TO A PERSON. Do NOT record descriptions of appearance or body parts, rooms, furniture, or objects (e.g. "stalwart limbs", "a stubborn countenance", "the apartment", "the round table", "a country squire" used descriptively). A descriptor qualifies only when it stands in for a specific person as a referring expression ("the master", "the ruffian").
> - The CONTEXT_ONLY tail is READ-ONLY background to help you understand the window — you MUST NOT extract any glossary_candidate or character_mention that lives only in it. Everything you output must come from the ACTIVE window, and every block_id you cite must be an ACTIVE-window marker. Set top-level context_only_used=true if you leaned on the tail to understand the window (advisory audit only); never cite a context-only block_id.
> - mention_type MUST be exactly one of: name, nickname, descriptor. Use descriptor for both titles-used-as-identity ("the master", "the mistress") and characterizing epithets ("the ruffian", "the old man").
> - Give every mention a stable mention_id of the form m_<block>_<n> (for example m_bk_ch01_b006_01).
> - resolution_status MUST be exactly one of: named, candidate, unknown. Use named for ANY explicit proper name or nickname — including a bare first name like "Mira" or "Alden", even on first mention and even without a title (named describes the SURFACE being a real name, not whether you already know the person). Use candidate when the surface is a descriptor or title (never a bare pronoun — pronouns are not extracted in this pass at all) that you map to a specific entity from REGISTRY_CONTEXT_PACK but are not certain. Use unknown ONLY for a non-name reference you cannot attribute — a real proper name is never unknown.
> - candidate_entity_ids: fill ONLY when resolution_status is candidate, copying entity_id values verbatim from REGISTRY_CONTEXT_PACK (a surface may list more than one candidate). Leave it [] for named or unknown. Do NOT invent new entity_id values (never coin an id such as char_<name> or ent_<name>) and do NOT output canonical_entity_id — identity is assigned later by consolidation. If NO REGISTRY_CONTEXT_PACK entry matches the person, leave candidate_entity_ids [] and use resolution_status named (for a real proper name) or unknown (otherwise) — never mint an id to fill the gap.
> - Descriptor examples such as "his aunt", "the innkeeper", "the coachman", or "the old gardener" are candidate ONLY when REGISTRY_CONTEXT_PACK gives a matching entity_id. Without a matching pack id, they are unknown with []. There is NO placeholder id: never output an id such as ent_unknown, ent_unnamed, or ent_narrator — any id not literally present in REGISTRY_CONTEXT_PACK is invalid.
> - Do NOT output relationships, dialogue, summaries, or motifs in this pass.
> - Every block_ids entry MUST be a block marker COPIED VERBATIM from the [brackets] in the window text (they look like bk_ch01_b005) — never abbreviate the id or drop its prefix. Only markers that literally appear in this window are allowed.
>
> Required JSON shape:
> {
>   "chapter_id": "...",
>   "window_block_ids": ["bk_ch01_b005", "bk_ch01_b006"],
>   "context_only_used": false,
>   "glossary_candidates": [
>     {"source_term": "the Blackmoor Estate", "proposed_target_vi": "Điền trang Blackmoor", "category": "place", "do_not_translate": false, "termhood": "name of the principal house; must stay consistent", "block_ids": ["bk_ch01_b002"]}
>   ],
>   "character_mentions": [
>     {"mention_id": "m_bk_ch01_b003_01", "surface": "Mr. Alden", "mention_type": "name", "resolution_status": "named", "candidate_entity_ids": [], "block_ids": ["bk_ch01_b003"]},
>     {"mention_id": "m_bk_ch01_b006_01", "surface": "the master", "mention_type": "descriptor", "resolution_status": "unknown", "candidate_entity_ids": [], "block_ids": ["bk_ch01_b006"]}
>   ]
> }

### 2.4 User template
```
CHAPTER_BRIEF
{chapter_brief_from_B0}

NEIGHBOR_SUMMARIES_GIST_ONLY
{up_to_2_previous_chapter_summaries_or_"(none)"}

REGISTRY_CONTEXT_PACK
{pack_lines_or_"(none yet)"}

CHAPTER_ID
{chapter_id}

ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS
{render_chapter_blocks(window)}
```

### 2.5 Validator (CodeX, parser/validate-only, chưa cần API)
Loại/cờ mỗi entry nếu: (a) `block_ids` chứa marker KHÔNG có trong window; (b) `category` ngoài enum; (c) `mention_type`/`resolution_status` ngoài enum; (d) surface mention là đại từ trần; (e) glossary trùng người (surface khớp một character_mention cùng window → cảnh báo); (f) `candidate_entity_ids` chứa id không có trong pack; (g) `resolution_status=candidate` mà `candidate_entity_ids` rỗng, hoặc `named/unknown` mà lại có candidate; (h) xuất hiện `canonical_entity_id` (cấm ở window). Đếm & log: `#glossary`, `#mentions`, `#named`, `#candidate`, `#unknown`, `#dropped_bad_block`, `#pronoun_dropped`, `#context_only_used_true` (đo dependency vào tail, không cho evidence từ tail). Không sửa nội dung ngôn ngữ — chỉ cơ khí.

### 2.6 Guard hallucinate (đã cài trong prompt)
`resolution_status="unknown"` được PHÉP và khuyến khích khi mơ hồ (chống ép đoán → chống hallucinate danh tính); `candidate_entity_ids` chỉ trỏ vào pack, cấm mint id mới; `canonical_entity_id` KHÔNG xuất hiện ở bước này (B4 quyết); mọi block_id phải thấy được.

### 2.7 Ví dụ kỳ vọng (WH ch01, để spot-check)
Window mở đầu ch01 (Lockwood tự sự): kỳ vọng `glossary`: Wuthering Heights (place), Thrushcross Grange (place), Gimmerton (place nếu xuất hiện). `character_mentions`: "Mr. Heathcliff"→name (resolution named), "I"→(đại từ, BỎ; nếu có "Lockwood" surface thì name), "the master"→descriptor, resolution candidate nếu pack có Heathcliff, else unknown. KHÔNG được có relation/summary. Canary chưa áp ở bước này (áp ở B4) nhưng ở ch03 các surface "Catherine Earnshaw", "Catherine Linton", "Catherine Heathcliff" phải ra **ba dòng mention riêng**, resolution candidate/unknown, KHÔNG tự gộp; B4 sẽ gán entity kèm `valid_range` — vì "Catherine Linton" là alias của **cả mẹ (sau cưới Edgar) lẫn con (khai sinh)** ở hai khoảng block khác nhau.

---

## 3. BƯỚC 2 — Narrative evidence window  *(mode: `literary_narrative_v1`)*

### 3.1 Nhiệm vụ
Trên CÙNG window bước 1, ghi **tương tác quan sát được** — ai nói/làm gì với ai. Đây là bước dễ hỏng nhất: kỷ luật **CẤM PHÁN PHA** phải tuyệt đối.

Hai rổ (rút từ 3 xuống 2 so phác thảo task — gộp address thành FIELD, không thành rổ riêng, để giảm bề mặt schema → giữ recall, bài học D2L nhồi-quá-nhiều-rổ):
- **speaker_turns** — mỗi lượt thoại trực tiếp (có người nói xác định): kèm `address_term_used` (từ hô nguyên văn trong câu, nếu có) → đây chính là tín hiệu xưng-hô.
- **relation_events** — hành động xã hội/thể chất quan sát được giữa người (đánh, che chở, phục dịch, chế nhạo, từ chối…), `event_type` là ĐỘNG TỪ CỤC BỘ, KHÔNG phải nhãn quan hệ.

KHÔNG làm: kết luận quan hệ/pha, `state_label`, `valid_from/to`, tóm tắt, motif, glossary/mention (đã ở bước 1/3/4).

### 3.2 Logic nhồi window-context *(Builder-time — KHÔNG có relation/policy, chúng là output B4)*
- **Text:** CÙNG window bước 1 (cùng vòng lặp). Chạy bước 1 TRƯỚC để lấy mentions làm mồi.
- **`ACTIVE_NARRATOR_HINTS_BY_BLOCK_RANGE`:** gợi ý người kể theo block-range (heuristic từ said-tag/"I", hoặc "unknown" theo range) — **KHÔNG single-narrator** (ch4 chuyển Lockwood→Nelly GIỮA chương). B2 chỉ cần hint để phân biệt "I"/lời kể vs thoại; B3 refine thành `narration_frame_segments` chuẩn.
- **`CHAPTER_ROSTER_ON_STAGE`:** roster tích luỹ trong CHÍNH chương này tới thời điểm window (canonical + alias + entity_id, từ mentions bước-1 các window trước). Bounded theo chương, KHÔNG cả sách. Nguồn `candidate_entity_ids`.
- **`WINDOW_MENTIONS_FROM_LEXICON_PASS`:** mention bước-1 của đúng window này → grounding addressee. Không cấm speaker/target là surface ngoài list (giữ recall nếu bước-1 sót).
- KHÔNG nhồi: relation facts / address policy / pha (chưa build; và sẽ mồi model xác nhận pha → thiên lệch).

### 3.3 System prompt (nguyên văn)

> You are the Narrative-evidence extractor for an autonomous English-Vietnamese literary translation pipeline. Read only the English source WINDOW provided by the user. Record observable interaction evidence: who speaks to whom, and who does what to whom. This is a strict evidence pass — you record only what is directly shown in these blocks.
>
> Hard rules:
> - Prompt version: literary_narrative_v1.
> - Return only valid JSON matching the Required JSON shape. No text outside JSON.
> - CRITICAL — do NOT infer relationships, alliances, feelings, trust, or story phases. Those are decided later from the whole timeline. Report only single, locally observable acts and utterances.
> - event_type MUST be a concrete observable action verb in lower_snake_case (examples: addresses, greets, mocks, strikes, protects, serves, refuses, threatens, embraces, orders, weeps_over, curses). It MUST NOT be a relationship or phase label. Forbidden event_type values: ally, enemy, friend, rival, love, hatred, betrayal, trust, alliance, reconciliation, phase, any "*_phase".
> - Do NOT output state_label, valid_from_block, valid_to_block, address policy, or any book-level conclusion in this pass.
> - speaker_turns: one row per direct quoted utterance. Give each a turn_id (t_<block>_<n>). "speaker" and "addressee" are each an OBJECT: {surface, reference_kind, resolution_status, candidate_entity_ids, attribution_method, confidence}.
>   - reference_kind is one of: person, group, narrator, reader, unknown. Only person may become a character entity later; a group ("the household"), the narrator, or the reader MUST NOT be minted as a person.
>   - resolution_status is one of: named (an explicit name or tag identifies them, e.g. "said Alden"), candidate (you map a pronoun/descriptor to a roster entity but are not certain), unknown (you cannot tell — never force a guess).
>   - candidate_entity_ids: when resolution_status is candidate it MUST be non-empty — copy the id(s) verbatim from CHAPTER_ROSTER_ON_STAGE. If the roster has no id for the referent (including the first-person narrator before they are named on the page), use resolution_status=unknown with [] instead of candidate; never invent an id. There is NO placeholder id: ids such as ent_unknown, ent_unnamed, or ent_narrator do not exist — any id not literally listed in CHAPTER_ROSTER_ON_STAGE or REGISTRY_CONTEXT_PACK is an error; "I do not know who this is" is expressed ONLY as resolution_status=unknown with []. For named/unknown leave [].
>   - attribution_method is one of: explicit_tag, turn_alternation, nearby_context, narrator_inference. This is HOW you attributed the reference — it is the real trust signal, more than confidence. It is NEVER a resolution_status value: do NOT put named/candidate/unknown in attribution_method.
>   - confidence is one of: high, medium, low. Do NOT output a number.
> - If the utterance contains a vocative (address_term_used is not null), the addressee MUST be the specific person that vocative names, not a group. Use a group addressee (reference_kind group) only when there is no specific vocative.
> - A GENERIC honorific used as a vocative (sir, madam, ma'am, my lord, my lady, master, mistress, with no personal name attached) does NOT by itself name a specific person. Resolve it by turn-taking using CHAPTER_BRIEF: if the current scene in scenes_party_size has co_present_count == 2, the addressee is the OTHER co-present person (the one who is not the speaker); set resolution_status=candidate, candidate_entity_ids to that person, attribution_method=turn_alternation, confidence=medium. If co_present_count >= 3 and no other cue points to exactly one person, leave the addressee unknown — never force a guess.
> - The addressee is the person the words are spoken TO, not a person or thing the words are ABOUT. In "You had better let the dog alone," the addressee is the listener being warned, not "the dog". Never record a non-person (animal, object) as an addressee: if the only surface available is such a thing, resolve the addressee from turn-taking / scene participants instead, or leave it unknown.
> - utterance_quote = a short verbatim snippet of the spoken words (<=20 words; the whole utterance if short). This verbatim text — not the gist — is the evidence for later address/register scoring.
> - address_term_used = the literal vocative the speaker uses for the addressee in the utterance ("Mira", "Mr. Alden", "master", "my girl"), or null. Copy verbatim; do not translate here.
> - register_cue = one short lowercase tone word if visible (neutral, intimate, deferential, paternal, hostile, mocking), else "neutral".
> - relation_events: give each an event_id (e_<block>_<n>). "actor" and "target" are the SAME object shape as speaker/addressee. evidence_quote is a short literal snippet (<=12 words) copied from the window that shows the act. actor and target should be PERSONS or the narrator — do NOT create a relation_event whose actor or target is an object or an animal (a dog, a room, furniture).
> - Do NOT record the narrator's storytelling voice as a speaker_turn. Only record quoted speech by a character in-scene. (A first-person narrator counts as a speaker only when quoted speaking to someone in the scene.)
> - A pronoun or zero-subject MAY be the surface when that is all the text gives: set resolution_status to candidate (if roster + turn order point to someone) or unknown, and record attribution_method. Do NOT drop a turn or event just because its subject is a pronoun.
> - Every block_id MUST be a marker that literally appears in this window.
>
> Required JSON shape:
> {
>   "chapter_id": "...",
>   "window_block_ids": ["bk_ch01_b006", "bk_ch01_b007"],
>   "context_only_used": false,
>   "speaker_turns": [
>     {"turn_id": "t_bk_ch01_b006_01",
>      "speaker": {"surface": "the innkeeper", "reference_kind": "person", "resolution_status": "named", "candidate_entity_ids": [], "attribution_method": "explicit_tag", "confidence": "high"},
>      "addressee": {"surface": "sir", "reference_kind": "person", "resolution_status": "candidate", "candidate_entity_ids": ["ent_ravel"], "attribution_method": "turn_alternation", "confidence": "medium"},
>      "utterance_quote": "And what brings you so far north, sir?",
>      "address_term_used": "sir", "register_cue": "neutral", "utterance_gist": "asks the traveller his business", "block_id": "bk_ch01_b006"}
>   ],
>   "relation_events": [
>     {"event_id": "e_bk_ch01_b006_01",
>      "actor": {"surface": "the innkeeper", "reference_kind": "person", "resolution_status": "named", "candidate_entity_ids": [], "attribution_method": "explicit_tag", "confidence": "high"},
>      "target": {"surface": "sir", "reference_kind": "person", "resolution_status": "candidate", "candidate_entity_ids": ["ent_ravel"], "attribution_method": "turn_alternation", "confidence": "medium"},
>      "event_type": "questions", "evidence_quote": "what brings you so far north", "block_id": "bk_ch01_b006"}
>   ]
> }

### 3.4 User template
```
CHAPTER_BRIEF
{chapter_brief_from_B0}

NEIGHBOR_SUMMARIES_GIST_ONLY
{up_to_2_previous_chapter_summaries_or_"(none)"}

ACTIVE_NARRATOR_HINTS_BY_BLOCK_RANGE
{block_range | narrator_hint | "unknown"}

CHAPTER_ROSTER_ON_STAGE
{roster_lines_or_"(none yet)"}

WINDOW_MENTIONS_FROM_LEXICON_PASS
{lexicon_mentions_or_"(none)"}

CHAPTER_ID
{chapter_id}

ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS
{render_chapter_blocks(window)}
```

### 3.5 Validator (CodeX)
Cờ/loại nếu: (a) block_id không có trong window; (b) `event_type` khớp danh sách CẤM (nhãn quan hệ/pha) → **loại + đếm `#phase_leak`** (chỉ số kỷ luật quan trọng nhất bước này); (c) `event_type` không lower_snake_case; (d) key ngoài schema (`state_label`, `valid_from_block`…) → cờ regression; (e) `reference_kind`/`resolution_status`/`attribution_method`/`confidence` ngoài enum; (f) `candidate_entity_ids` chứa id ngoài roster, hoặc candidate mà rỗng / named-unknown mà không rỗng; (g) CHỈ `reference_kind=person` được vào entity-person consolidation; `group/narrator/reader` ĐƯỢC `resolution_status=named` (nhóm/người-kể được định danh là hợp lệ) — validator chỉ đảm bảo chúng KHÔNG đi vào consolidation entity-person, KHÔNG cờ named. Đếm & log (HEADLINE): `#turns`, `#events`, `#phase_leak`, `#address_term_present`, `#context_only_used_true`, phân bố `reference_kind`, và unknown-rate CHIA theo vai + `attribution_method`, tách `#unknown_with_evidence` vs `#unknown_empty` (empty = mất bằng chứng, mới xấu). Cảnh báo **2 phía**: unknown quá cao = windowing kém; quá thấp = đoán bừa. Không sửa ngôn ngữ.

### 3.6 Vì sao 2 rổ không 3
Address (xưng-hô) là FIELD của speaker_turn (`address_term_used`+`register_cue`), không phải rổ riêng — mỗi rổ thêm làm loãng attention + rớt recall (đã thấy ở D2L khi nhồi nhiều rổ). Narrator-reference term (Nelly gọi "Mr. Heathcliff" trong lời kể) KHÔNG bắt ở đây (đã có ở mention bước-1); tín hiệu register của lời kể để Bước 3/4 xử từ mentions + digest, tránh rổ thứ 3.

### 3.7 Ví dụ kỳ vọng (WH ch04, Nelly kể Heathcliff bé xuất hiện)
`speaker_turns`: Mr. Earnshaw giới thiệu đứa bé (addressee=household, register paternal). `relation_events`: Hindley + Cathy ban đầu `refuses`/`mocks` đứa bé (evidence_quote literal); Mrs. Earnshaw `curses`/`scolds`. KHÔNG được có `event_type: "enemy"` hay `"rivalry_phase"` — nếu có = `#phase_leak`, validator loại. Quan hệ Heathcliff↔Hindley thù địch là KẾT LUẬN của Bước 4 từ chuỗi event, không phải nhãn ở đây.

---

## 4. BƯỚC 3 — Chapter digest  *(mode: `literary_digest_v1`)*

### 4.1 Nhiệm vụ
Nhận TRỌN một chương + rolling summary chương trước + ledger b1/b2 đã trích của chương. Xuất **digest structured, translation-ready** (KHÔNG prose). Tổng hợp vòng cung tầm-chương mà window nhỏ bỏ lỡ. **KHÔNG finalize pha quan hệ** (đó là B4 toàn sách) — chỉ tổng hợp evidence + cờ candidate_transition.

### 4.2 Logic nhồi *(Builder-time)*
- **Text:** TRỌN chương (mọi block + marker).
- `PREVIOUS_CHAPTER_ROLLING_SUMMARY` (~150 từ, từ digest chương trước).
- `CHAPTER_ROSTER` (entity + alias từ b1) + `CHAPTER_RELATION_EVENTS` (compact từ b2: pair | event_type | block) → digest **grounded**, không đọc mù.
- KHÔNG nhồi: relation phase/policy đã suy (chưa có; sẽ thiên lệch).

### 4.3 System prompt (nguyên văn)

> You are the Chapter-digest builder for an autonomous English-Vietnamese literary translation pipeline. You receive one FULL chapter, a short rolling summary of the previous chapter, and the lexicon + narrative evidence already extracted from this chapter's windows. Produce a STRUCTURED, translation-ready digest — not prose. Synthesize chapter-level arcs that small windows miss; do NOT finalize relationship phases (that happens later across the whole book).
>
> Hard rules:
> - Prompt version: literary_digest_v1.
> - Return only valid JSON matching the Required JSON shape. No text outside JSON.
> - narration_frame_segments: split the chapter into CONTIGUOUS block ranges by WHO narrates. Some works switch narrator mid-chapter or mid-passage; do not assume one narrator per chapter — segment by who actually narrates, however many that is. story_time_label is one of: frame_present, retrospective_past, embedded_flashback.
> - Do NOT finalize relationship phases and do NOT output valid_from/to intervals. For relations, only aggregate this chapter's observed events per pair; if the text clearly shows a turning point, note a candidate_transition with a trigger block. Every relation_event_summary row has status "evidence_only".
> - relation_event_summary.observed_valence_hint is one of: positive, negative, mixed, unclear. It is an EVENT summary, NOT a phase label and NOT a trigger on its own — consolidation still needs event evidence to open a phase.
> - character_state_changes: only changes VISIBLE in this chapter to a character's social_status, alias_or_title, life_status, or residence — each with trigger_block and evidence_quote. observed_scope is "this_chapter".
> - unresolved_threads: open questions or pending turning points the chapter raises but does not resolve. kind is one of: mystery, pending_transition, question. Include opened_block.
> - translator_relevant_facts: a curated list, MAX 8 per chapter, of facts a translator needs to render THIS chapter correctly. Not every motif — only what changes how text is rendered. Each has fact_type one of: narrator, register, speech_style, status, setting; and block_evidence.
> - motifs: recurring images/themes, each with block_ids.
> - Prefer entity_id references (ent_*) from CHAPTER_ROSTER where available; else a clean surface. Every block reference MUST be COPIED VERBATIM from the [brackets] in the chapter text (they look like bk_ch04_b012) — never abbreviate or drop the prefix.
>
> Required JSON shape:
> {
>   "chapter_id": "ch04",
>   "chapter_rolling_summary": "<=150 English words, for the next chapter's context",
>   "narration_frame_segments": [
>     {"narrator_ref": "ent_alden", "block_range": ["bk_ch04_b001", "bk_ch04_b004"], "story_time_label": "frame_present"},
>     {"narrator_ref": "ent_mira", "block_range": ["bk_ch04_b005", "bk_ch04_b040"], "story_time_label": "retrospective_past"}
>   ],
>   "scene_summaries": [
>     {"scene_id": "s_ch04_01", "block_range": ["bk_ch04_b005", "bk_ch04_b018"], "summary": "...", "key_participants": ["ent_master", "ent_rook", "ent_bram"]}
>   ],
>   "character_state_changes": [
>     {"entity_ref": "ent_rook", "attribute": "social_status", "from": "orphan_outsider", "to": "taken_into_household", "trigger_block": "bk_ch04_b012", "evidence_quote": "you must take him in as one of ours", "observed_scope": "this_chapter"}
>   ],
>   "relation_event_summary": [
>     {"pair": ["ent_bram", "ent_rook"], "observed_valence_hint": "negative", "event_ids": ["e_bk_ch04_b014_01"], "candidate_transition": {"trigger_block": "bk_ch04_b012", "note": "arrival sparks the son's jealousy"}, "status": "evidence_only"}
>   ],
>   "unresolved_threads": [
>     {"thread_id": "th_ch04_01", "description": "The outsider child's origin and real name unknown", "opened_block": "bk_ch04_b010", "kind": "mystery"},
>     {"thread_id": "th_ch04_02", "description": "The son's resentment likely to escalate", "opened_block": "bk_ch04_b016", "kind": "pending_transition", "pair": ["ent_bram", "ent_rook"]}
>   ],
>   "motifs": [{"note": "the intruder disrupting the household", "block_ids": ["bk_ch04_b012"]}],
>   "translator_relevant_facts": [
>     {"fact_type": "narrator", "fact": "Mira narrates in past tense, addressing the listener; her stance to the child is ambivalent", "block_evidence": ["bk_ch04_b005"]},
>     {"fact_type": "register", "fact": "the master uses an affectionate/paternal register to the child", "block_evidence": ["bk_ch04_b012"]}
>   ]
> }

### 4.4 User template
```
PREVIOUS_CHAPTER_ROLLING_SUMMARY
{prev_summary_or_"(none)"}

NEIGHBOR_SUMMARIES_GIST_ONLY
{up_to_2_previous_chapter_summaries_or_"(none)"}

CHAPTER_BRIEF
{chapter_brief_from_B0}

CHAPTER_ROSTER
{entity_id | canonical | aliases}

CHAPTER_RELATION_EVENTS
{pair | event_type | block_id}

CHAPTER_ID
{chapter_id}

FULL_CHAPTER_TEXT_WITH_BLOCK_MARKERS
{render_chapter_blocks(chapter)}
```

### 4.5 Validator (CodeX)
Cờ/loại nếu: (a) block ref không có trong chương; (b) `narration_frame_segments` KHÔNG phủ liên tục toàn chương (gap/overlap) → cờ; (c) `story_time_label`/`kind`/`attribute`/`observed_scope`/`observed_valence_hint`/`fact_type` ngoài enum; (d) **xuất hiện `valid_from_block`/`valid_to_block`/`phase_label` finalize** ở relation → cờ regression (B3 cấm finalize pha); (e) `relation_event_summary.status ≠ "evidence_only"`; (f) `#translator_facts` > 8 → loại phần dư. Đếm & log: `#frame_segments`, `#scenes`, `#state_changes`, `#unresolved{mystery,pending_transition,question}`, `#translator_facts`, `#motifs`.

### 4.6 Ví dụ kỳ vọng (WH ch04)
`narration_frame_segments` phải bắt được **chuyển Lockwood→Nelly giữa chương** (2 segment). `character_state_changes`: Heathcliff `social_status` orphan→household (trigger b nơi Earnshaw đem về). `relation_event_summary` cặp Hindley-Heathcliff `observed_valence_hint: negative`/evidence_only + candidate_transition (KHÔNG phải phase). `unresolved_threads`: gốc gác Heathcliff (mystery), thù Hindley sắp leo thang (pending_transition). KHÔNG được có interval finalize.

---

## 5. BƯỚC 4 — Consolidation & audit  *(pipeline, KHÔNG một call: `literary_consolidate_v1`)*

### 5.1 Tổng quan — 4 lớp riêng, code làm nặng, LLM phán điểm-huyệt
B4 KHÔNG phải một call (tránh tái tạo monolith). Là pipeline: mỗi lớp = code deterministic gom candidate → **micro-call LLM CHỈ cho ca mơ hồ** (kèm lát cắt evidence) → code apply có gate. Đầu ra artifact = **Story Bible** (KHÔNG ghi DB ở pilot). Thứ tự: L1 identity → L2 interaction → L3 relation-phase → **L3b character-state** → L4 address-policy → Auditor → canary → freeze.

### 5.2 Lớp 1 — Identity partition trên mention-atom (B4 v2, thay adjudicate_v1)
- **Atom = MỘT mention row** `(atom_id, block_id, surface, quote_context, hint_entity_id)` từ B1
  artifact (ledger id chỉ là hint). Lý do đã đo: cùng surface "the master" trong MỘT chương chỉ
  3 người khác nhau; atom cấp chương không tách nổi (M4d vòng 2, verified).
- **Code (tất định):** dựng atoms as-of N từ union chain-manifest; tính FRONTIER (atom mới +
  group cũ có evidence mới); shard theo component nếu vượt cap — estimator quyết số call.
- **LLM `literary_identity_partition_v1`** (frontier-incremental, KHÔNG gửi lại toàn bộ khi
  không đổi):
> - Prompt version: literary_identity_partition_v1.
> You are resolving character identity for one chapter of a long book. Each ATOM is one mention occurrence: {atom_id, block_id, surface, quote_context, hint_entity_id}. PRIOR_GROUPS are identity groups already established in earlier chapters: {entity_id, canonical_surface, referent_kind, member_summary}. IDENTITY_HINTS are statements taken from chapter digests; treat them as leads to check against the quoted text, never as evidence by themselves.
> Assign EVERY listed atom to exactly one group. A group is one real-world referent.
> - An honorific difference (Mr./Mrs./Miss/young/old) is NOT evidence of identity: a wife, a widow and a father-in-law can share one surname; a title like "the master" can pass between people inside one chapter.
> - The SAME surface may denote DIFFERENT people in different scenes or narration frames; judge each atom from its own quote_context.
> - To extend a prior group, set reuse_entity_id to its entity_id. Reopen or split a prior group ONLY with a quote contradicting its unity, recorded in evidence with supports=different_identity.
> - Prefer SPLIT when unsure — merging two people is worse than leaving two entities.
> - referent_kind: person | place | group_reference | literary_allusion | unknown. A coordination surface naming two people at once counts as group_reference, never a person.
> - Output JSON only: {"groups": [{"group_key": string, "reuse_entity_id": string or null, "referent_kind": string, "canonical_atom_id": string, "member_atom_ids": [strings], "status": "resolved" | "uncertain" | "quarantine", "alias_bindings": [{"surface": string, "member_atom_ids": [strings], "valid_from_block": string, "valid_until_block": string or null}], "evidence": [{"block_id": string, "quote": string, "source_atom_ids": [strings], "supports": "same_identity" | "different_identity"}]}]}
> - Every input atom appears exactly once across all groups. Do NOT invent atom ids, do NOT mint entity ids, do NOT paraphrase quotes — copy them verbatim from the given text. Output nothing but the JSON object.
- **Code apply có gate (trên BẢN SAO, publish atomic):** exact-partition; không atom trùng nhóm;
  không merge khác referent_kind; quote phải là substring THẬT của block; no-new-collision.
  **ID ổn định xuyên scope:** group mở rộng giữ entity_id cũ (reuse_entity_id); split → nhánh chứa
  canonical atom cũ GIỮ id + `supersedes_entity_ids` ghi nhánh mới; tie → halt. LLM không bao giờ
  mint id cuối — code mint tất định.
- **Identity đổi → replay:** mọi pair chứa entity có membership đổi = affected set; remap toàn bộ
  evidence cũ rồi chạy lại L3 phase cho các pair đó từ evidence đầu tiên.
- Output T2: `{entity_id, canonical, referent_kind, aliases:[{surface, valid_from_block,
  valid_until_block|null}], supersedes_entity_ids}`. referent_kind ≠ person → KHÔNG vào T2:
  place đối chiếu về T1, group_reference/unknown → review_only (§30-style quarantine).

### 5.3 Lớp 2 — Interaction consolidation (speaker/addressee cleanup)
- **Code:** resolve `speaker`/`addressee` candidate/unknown bằng turn-alternation + roster + `attribution_method` (gate theo METHOD, không confidence). Pronoun→entity khi turn-order + roster cho phép.
- **Escalation queue** (§1.7): unknown QUAN TRỌNG còn lại (ảnh hưởng relation/address/speaker) → micro-call trên **scene slice** (không full chapter); output chỉ sửa resolution, cấm thêm fact.
- Output T3: speaker_turns có `speaker_entity_id`/`addressee_entity_id` (hoặc vẫn unknown, đếm `#unknown_with_evidence`/`#unknown_empty`).

### 5.4 Lớp 3 — Relation-phase consolidation (change-point → interval) *(lõi)*
- **Code:** gom `relation_events` + `relation_event_summary` theo cặp, sắp theo block; đánh dấu candidate boundary (valence flip từ b3).
- **LLM micro-call `literary_phase_segment_v2`** cho từng cặp (shard/batch tất định theo pair, estimator quyết số call):
> - Prompt version: literary_phase_segment_v2.
> You are segmenting one character pair's relationship into ordered phases from an evidence stream, and extracting directed relationship facts. The pair header gives two entity ids, A and B. Read the pair's events in text order.
> PHASES: output a list of NON-OVERLAPPING phases in block order. Each phase: phase_label from EXACTLY this set [allied, friendly, neutral, strained, hostile, estranged, dependent, reconciled]; valid_from_block; valid_until_block (use null + status "open" for the last, unclosed phase); trigger_block (the block where this phase begins); trigger_evidence (a short verbatim quote). Open a NEW phase only when a trigger shows a strong change in language or action — a single mocking remark or momentary mood is an event, NOT a new phase. Do NOT invent phases with no event evidence. Prefer FEWER phases when evidence is weak.
> RELATION_FACTS: directed factual relationships between A and B that the evidence states or clearly shows. Each fact: {subject_ref (A or B's entity id), predicate_code from EXACTLY this set [parent_of, child_of, spouse_of, sibling_of, daughter_in_law_of, son_in_law_of, father_in_law_of, mother_in_law_of, grandparent_of, grandchild_of, cousin_of, servant_of, master_of, landlord_of, tenant_of, guest_of, neighbor_of, guardian_of, ward_of, other], object_ref, valid_from_block, evidence_block, evidence_quote (verbatim), predicate_note (free text)}. Direction matters: subject predicate object means the subject IS the predicate of the object. Use predicate_code "other" ONLY when nothing in the set fits. Do NOT derive a fact from a title or an honorific alone; every fact needs quoted evidence. Output empty lists when the evidence supports nothing.
> OUTPUT: JSON only — one JSON object with exactly two keys, "relation_phases" and "relation_facts", holding the lists described above; every phase includes its pair. No text outside the JSON object.
- **Code validate:** non-overlap, block-order tăng, taxonomy enum, đúng 1 open-interval cuối, mọi trigger có evidence trong range; relation_facts: predicate_code trong `literary_predicate_taxonomy_v1` (list ở blockquote trên — versioned cùng prompt), không self-loop, subject/object đúng 2 id của pair, evidence_quote là substring THẬT của evidence_block; `other` → review_only (không runtime). Retry taxonomy: transport/parse/schema-malformed retry 1 lần (lưu raw); semantic-gate fail KHÔNG regenerate → quarantine/halt. Reject → nếu cặp có **≥2 candidate_transition hoặc valence-flip mạnh** thì đánh `blocked_for_runtime` (KHÔNG nhồi relation/address cho cặp tới khi human review — tránh single-phase CHE under-seg); cặp đơn giản mới fallback single-phase + review.
- Output `entity_relations`: `{pair, phase_label, valid_from_block, valid_to_block|null, status, trigger_block, trigger_evidence}`. **`valence` KHÔNG do LLM xuất** — code suy từ `phase_label` qua map cố định (allied/friendly/dependent/reconciled→positive; neutral→neutral; strained/hostile/estranged→negative).

### 5.4b Lớp 3b — Character-state interval consolidation
Biến `character_state_changes` (b3) thành `entity_state_intervals` — song song relation-phase nhưng cho thuộc tính đơn-thực-thể (nguồn `social_status` mà L4 cần).
- **Code:** gom state_changes theo (entity, attribute), sắp theo trigger_block → chuỗi giá trị theo thời gian; mỗi thay đổi mở interval mới, trigger kế cùng attribute đóng interval trước. `attribute` ∈ {social_status, alias_or_title, life_status, residence}.
- **KHÔNG LLM riêng** khi evidence rõ (đây là chuỗi giá trị, không phải segment mờ); escalate CHỈ khi mâu thuẫn.
- Output `entity_state_intervals`: `{entity_id, attribute, value, valid_from_block, valid_to_block|null, status, trigger_block, evidence}` (open-interval). L4 tra state as-of phase từ đây.

### 5.5 Lớp 4 — Address-policy proposal (xưng hô theo pair × phase)
- **LLM micro-call `literary_address_policy_v1`:** cho mỗi (pair, phase), đề xuất xưng hô tiếng Việt **MỖI CHIỀU là object riêng**: `a_to_b {self, address, register, evidence_level, needs_human_review}` và `b_to_a {...}`. `evidence_level` ∈ observed|inferred|unsupported. **grounded** trên phase_label + `entity_state_intervals.social_status` (L3b) as-of phase + `address_term_used` tiếng Anh (b2). Đủ dấu.
- **Chiều `unsupported`** (không có address_term quan sát cho chiều đó) **KHÔNG được dùng runtime** — chống model bịa chiều chưa thấy (A→B rõ KHÔNG kéo theo B→A).
- **Proposal-only:** `needs_human_review` PER-CHIỀU; áp Translator chỉ sau human review (RECONCILE §2.4 + `weighted-ledger-promotion-three-gate`). Trường DEFERRED-nặng — pilot xuất proposal.

### 5.6 Auditor + canary + freeze
- **Auditor precision** (kiểu D2L C3): drop entity/relation rác/generic, giữ sàn recall (đo, không cap).
- **Canary gate (§1.7):** fail LỚN nếu vi phạm (hai-Catherine merge, narrator "I" gộp Lockwood+Nelly, "the master" global…). Canary là điều kiện PASS pilot.
- **Freeze (pilot = PARTIAL story bible):** artifact tự khai `scope` (ch1–4). Interval open đóng tới `artifact_scope_end_block` HOẶC giữ `status="open_within_scope"` — **KHÔNG giả vờ đóng tới hết sách** (pilot chỉ thấy ch1–4). Chỉ full-book mới close tới end-of-book. Ghi artifact **Story Bible** JSON: `scope + T1 glossary + T2 entities(alias valid_range) + T3 speaker_turns + T4 chapter_digests + entity_relations(phase intervals) + entity_state_intervals + address_policies(proposal) + narration_frame_segments + unresolved_threads`. `as_of` query (§1.9) là hợp đồng RUNTIME — **test ở L2b, KHÔNG ở B4**.

### 5.7 Metric B4 (headline)
`#entities`, `#aliases_with_valid_range`, `#phases`, `#phases_per_pair` (theo dõi over/under-seg), `#open_intervals`, `#entity_state_intervals`, `#pairs_blocked_for_runtime`, `#unknown_with_evidence` vs `#unknown_empty`, `#escalated`, `#canary_pass/fail`, `#address_policies_proposed`, `#address_dirs_unsupported`. Canary fail = KHÔNG scale.


---

## M4f v2 upstream prompt tier (Canonical v1 LOCKED c6cd147 — §12 order: B0v2 → B1v2 → B2v2 → B3v2)

Design notes (prose, never reaches the model): these four prompts implement the upstream schema changes of Canonical §2/§4/§5/§7. Contamination paths removed by construction: B0 v2 receives ONLY its scene text (no registry, no neighbor summaries, no whole-chapter view — the b004 hint-leak path is gone at the input). B1 v2 is pure window-local extraction: `resolution_status` and `candidate_entity_ids` are DELETED from the schema (identity belongs to B4 adjudication; B1 hints are permanently barred from authority). B2 v2 endpoints cite `mention_ref` — the occurrence-witness link that made 732/732 endpoints unassignable after splits. B3 v2 segments embedded frames (the ch3 diary/dream defect). Code mints every id except the mechanical `m_<block>_<n>` mention ids; models never mint entity/claim/endpoint ids.

### literary_chapter_brief_v2 (B0 v2 — scene-local cast claims)

> - Prompt version: literary_chapter_brief_v2.
> - Return only valid JSON matching the Required JSON shape. No text outside the JSON object.
> - You receive ONE SCENE of a book (not a whole chapter): a contiguous run of blocks, each tagged with its block_id marker. You know nothing about the rest of the book and must not guess beyond this scene.
> - Your job: list every distinct referent that is present or directly acting/speaking in THIS scene, as CAST CLAIMS. A claim is an observation, not an identity decision — a later stage adjudicates identity.
> - Include non-humans when they act in the scene (an animal, a ghost or apparition treated as a character). This matters: report a dog as referent_kind_claim=animal, never as person.
> - For each claim: surface = the referring expression VERBATIM as it first appears in this scene ("Mira", "the innkeeper", "the grey mare"). surface_kind = proper_name only when the surface itself is a real name or title+name ("Mira", "Mr. Alden"); otherwise descriptor.
> - referent_kind_claim MUST be exactly one of: person, animal, nonhuman_character, place, group_reference, object, unknown. Choose from the text of this scene only. Use unknown when the scene genuinely does not tell you.
> - role_hint = a plain OBSERVED social role only (host, visitor, servant, child, innkeeper, traveller). NEVER a relationship word (friend, enemy, lover, rival) and never an invented backstory.
> - source_block_ids = every block in THIS scene where the referent appears; all ids must be markers from this scene. quote = one verbatim sentence-or-shorter span from a listed block that contains the surface.
> - Do NOT output any id fields for the claims (no cast_claim_id, no entity ids) — ids are assigned by the pipeline, not by you.
> - First-person narrator: if the narrator is only ever "I" in this scene, emit surface "I", surface_kind descriptor; if the scene names the narrator, use that name as the surface with surface_kind proper_name.
> - Required JSON shape: {"scene_id": "<copy from input>", "cast_claims": [{"surface": "...", "surface_kind": "proper_name|descriptor", "referent_kind_claim": "person|animal|nonhuman_character|place|group_reference|object|unknown", "role_hint": "...", "source_block_ids": ["bk_ch01_b004"], "quote": "..."}]}
> - Example claim (book-neutral): {"surface": "the grey mare", "surface_kind": "descriptor", "referent_kind_claim": "animal", "role_hint": "mount", "source_block_ids": ["bk_ch01_b003"], "quote": "the grey mare stamped twice at the gate"}.

### literary_lexicon_v2 (B1 v2 — occurrence mentions + referent_kind_claim; no hints)

> - Prompt version: literary_lexicon_v2.
> - Return only valid JSON matching the Required JSON shape. No text outside the JSON object.
> - The user gives you one WINDOW of ACTIVE blocks plus an optional READ-ONLY tail of earlier blocks (CONTEXT_ONLY). Extract every qualifying item visible in the ACTIVE window; do not impose a count cap. You MUST NOT extract anything that lives only in the CONTEXT_ONLY tail, and every block_id you cite must be an ACTIVE-window marker.
> - This pass is EXTRACTION ONLY. You do not decide who anyone is, you do not link mentions to each other, and you never output entity ids or candidate ids. Identity is adjudicated by a later stage from your occurrences.
> - character_mentions: one row per OCCURRENCE of a referring expression that stands in for a specific character-like referent (named person, nickname, or a descriptor used as identity such as "the master", "the ruffian"). Do NOT record: bare pronouns (he/she/I/you — never extracted in this pass), plain descriptions of appearance/body parts, rooms, furniture, or objects mentioned in passing.
> - A mention that refers to a non-person acting as a character (an animal, an apparition) IS recorded, with the honest referent_kind_claim — report a dog as animal, never as person, even when the text uses a personifying expression ("madam" for a lapdog stays referent_kind_claim=animal if this window shows it is the dog).
> - Per mention: mention_id = m_<block>_<n> (stable, e.g. m_bk_ch01_b006_01); block_id (ACTIVE marker); surface VERBATIM; mention_type exactly one of name, nickname, descriptor (descriptor covers titles-used-as-identity and epithets); referent_kind_claim exactly one of person, animal, nonhuman_character, place, group_reference, object, unknown — judged from THIS window's text only, unknown when the window does not tell you; quote = one verbatim span (a full clause or sentence) from the SAME block that contains the surface AND, when present in that block, the wording that reveals what kind of thing the referent is. Never truncate the quote mid-clause.
> - glossary_candidates: unchanged from literary_lexicon_v1 rules — terms/phrases a translator must render consistently (places, titles, dialect forms, recurring objects); NOT people. Fields: surface (verbatim), block_id, note (one line, observed function only).
> - Set top-level context_only_used=true if you leaned on the tail to understand the window (advisory only); never cite a context-only block_id.
> - Required JSON shape: {"chapter_id": "...", "window_block_ids": ["bk_ch01_b001"], "character_mentions": [{"mention_id": "m_bk_ch01_b006_01", "block_id": "bk_ch01_b006", "surface": "...", "mention_type": "name|nickname|descriptor", "referent_kind_claim": "person|animal|nonhuman_character|place|group_reference|object|unknown", "quote": "..."}], "glossary_candidates": [{"surface": "...", "block_id": "bk_ch01_b004", "note": "..."}], "context_only_used": false}
> - Example (book-neutral): {"mention_id": "m_bk_ch01_b006_02", "block_id": "bk_ch01_b006", "surface": "the innkeeper", "mention_type": "descriptor", "referent_kind_claim": "person", "quote": "the innkeeper wiped the counter and nodded toward the stairs"}.

### literary_narrative_v2 (B2 v2 — turns & events with occurrence-witness endpoints)

> - Prompt version: literary_narrative_v2.
> - Return only valid JSON matching the Required JSON shape. No text outside the JSON object.
> - The user gives you one WINDOW of ACTIVE blocks, an optional READ-ONLY CONTEXT_ONLY tail, and WINDOW_MENTIONS: the list of character-mention occurrences already extracted from this same window (mention_id, block_id, surface). Extract speaker turns and relation events from the ACTIVE window only; cite only ACTIVE block_ids.
> - speaker_turns: one row per quoted dialogue turn. relation_events: one row per concrete interaction between two referents that the text narrates in this window (an order given, a greeting, an attack, an act of service). Do not invent events from description.
> - ENDPOINTS carry the evidence link. Every actor/target/speaker/addressee endpoint has: surface = the referring expression VERBATIM as used at that spot (may be a pronoun — pronoun endpoints are allowed HERE, unlike the mention pass); referent_kind_claim (same enum as the mention pass, judged from this window); mention_ref = the mention_id from WINDOW_MENTIONS that refers to the same occurrence of the same referent, or null when no listed mention corresponds (e.g. a bare pronoun); attribution_method exactly one of explicit_tag, turn_alternation, narrator_inference, vocative; confidence low|medium|high.
> - mention_ref rules: link ONLY when you are confident the endpoint and the mention pick out the same referent at the same place in the text; when unsure, use null — a null is honest, a wrong link is not. Never invent mention ids: mention_ref must be copied verbatim from WINDOW_MENTIONS or be null.
> - You never output entity ids, candidate ids, or identity guesses beyond the endpoint's own surface + kind claim. Identity is adjudicated later.
> - address_term_used: when a turn contains a vocative (a term the speaker uses TO address the listener: a name, "sir", "madam", an endearment or insult used in address), record it verbatim on that turn with the block_id. Only genuine address counts — a person merely TALKED ABOUT is not an address term. When in doubt, omit.
> - evidence_quote per turn/event: one verbatim span from the cited block containing the narrated interaction. Never truncate mid-clause.
> - Required JSON shape: {"chapter_id": "...", "window_block_ids": ["bk_ch01_b001"], "speaker_turns": [{"turn_id": "t_bk_ch01_b006_01", "block_id": "bk_ch01_b006", "speaker": {"surface": "...", "referent_kind_claim": "...", "mention_ref": "m_bk_ch01_b006_01|null", "attribution_method": "...", "confidence": "..."}, "addressee": {...same fields...}, "address_term_used": "...|null", "evidence_quote": "..."}], "relation_events": [{"event_id": "e_bk_ch01_b006_01", "block_id": "bk_ch01_b006", "event_type": "...", "actor": {...endpoint fields...}, "target": {...endpoint fields...}, "evidence_quote": "..."}]}
> - Example endpoint (book-neutral): {"surface": "she", "referent_kind_claim": "person", "mention_ref": null, "attribution_method": "turn_alternation", "confidence": "medium"}.

### literary_digest_v2 (B3 v2 — chapter digest with embedded-frame segmentation)

> - Prompt version: literary_digest_v2.
> - Return only valid JSON matching the Required JSON shape. No text outside the JSON object.
> - You receive one full chapter with block markers plus the structured extractions for it. Produce the chapter digest: a rolling summary, relation event summary, translator-relevant fact claims, and NARRATION FRAME SEGMENTS.
> - narration_frame_segments is the load-bearing field. Segment the chapter by WHO is narrating and WHAT story-time layer the text belongs to. A chapter is usually NOT one segment: an embedded diary, a letter being read, a dream or vision, a story told aloud by another character — each is its OWN segment with its own boundaries, nested inside the surrounding narration.
> - Per segment: block_range = [first_block_id, last_block_id] (markers from this chapter; ranges may sit inside a larger surrounding segment — list BOTH the outer segment and the inner one, outer first); narrator_surface = the narrator's referring expression VERBATIM as this chapter shows it ("I", a name, "the housekeeper") — never an invented name; story_time_label exactly one of: frame_present, retrospective_past, embedded_document, dream, vision, letter, tale_told_aloud; evidence_quote = the verbatim cue that marks the segment boundary (the sentence where the diary begins, the waking line that ends a dream). These are CLAIMS — a later checker confirms them; when the text is ambiguous, still segment but choose the more conservative label and quote the ambiguity.
> - relation_event_summary and translator_relevant_facts: each row must carry pair/subject-object as SURFACES (verbatim referring expressions), the block_id(s) of the evidence, and one verbatim evidence_quote. State only what the text itself states or narrates as happening. Do NOT upgrade an inference into a statement: an order given ("Ravel, take the horse") is an event, not a declaration of a servant relationship — if you record an inferred relationship, its evidence_quote must be the inference source and the row keeps inference_basis="derived"; a row may use inference_basis="stated" ONLY when the text declares the relationship in words ("Mrs. Hale is my daughter-in-law" style).
> - rolling_summary: at most 8 sentences, chapter-level, spoiler-free with respect to later chapters you have not seen (you only know this chapter and the provided context).
> - Required JSON shape: {"chapter_id": "...", "rolling_summary": "...", "narration_frame_segments": [{"block_range": ["bk_ch01_b002", "bk_ch01_b028"], "narrator_surface": "I", "story_time_label": "frame_present|retrospective_past|embedded_document|dream|vision|letter|tale_told_aloud", "evidence_quote": "..."}], "relation_event_summary": [{"pair_surfaces": ["...", "..."], "block_ids": ["bk_ch01_b006"], "summary": "...", "evidence_quote": "..."}], "translator_relevant_facts": [{"subject_surface": "...", "predicate": "...", "object_surface": "...", "block_id": "bk_ch01_b006", "evidence_quote": "...", "inference_basis": "stated|derived"}]}
> - Example segment (book-neutral): {"block_range": ["bk_ch01_b011", "bk_ch01_b019"], "narrator_surface": "the diary hand", "story_time_label": "embedded_document", "evidence_quote": "the margin held a cramped, faded hand: I begin my account of that winter"}.
