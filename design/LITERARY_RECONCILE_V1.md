# LITERARY RECONCILE V1 — Đối chiếu bản khóa văn học (06-11/12) với bài học D2L (06-13 → 07-07)

> **Trạng thái:** **L0 ĐÃ DUYỆT 2026-07-08** — user chốt: gói §2.1–§2.5 ✅, ACS
> bắt buộc ✅, Q1 giữ-roadmap-nén-lịch ✅, Q5 2-pilot ✅, cache-metric LÀM /
> DITING để sau ✅. Còn mở: Q2 (probe quyết), Q3 (GVHD), Q4 (thực thi ở L1c),
> Q6 (spec quyết). Bước kế: TASK L1a–d.
>
> **Mục đích:** Track văn học (Treasure Island) đã được thiết kế chi tiết trong
> `THESIS_ARCHITECTURE_LOCK.md` **TRƯỚC** khi chặng D2L chạy. D2L (Builder v2,
> one-button, scoring stack, localizer cascade…) đã dạy nhiều bài mà bản lock chưa
> biết. Doc này đối chiếu TỪNG quyết định cũ với bài học mới → quyết **GIỮ /
> NÂNG CẤP / CHỐT LẠI** — nguyên tắc: *nâng cấp và bổ sung workflow D2L để lại,
> KHÔNG xây lại từ đầu*.
>
> **Thứ tự ưu tiên khi mâu thuẫn:** Directional Lock (GVHD) > LOCK > **doc này**
> (chỉ thắng LOCK ở mục có bằng chứng D2L ghi rõ) > V3 chi tiết.
> Khi một mục ở đây được chốt ✅, ghi ngược 1 dòng vào LOCK §10 changelog.

---

## 0. Nguyên tắc reconcile (khung tư duy)

1. **Kiến trúc KHÔNG đổi** — Builder → memory-pack → Translator → scoring → one-button
   giữ nguyên. Văn học = **profile mới + nội dung memory mới + metric bổ sung**.
2. **Hai lớp memory văn học** (khác chất, không chỉ khác lượng so với D2L):
   - **Lớp bounded-registry** (tên nhân vật, địa danh, thuật ngữ hàng hải/hư cấu):
     = "term registry của văn học" → toàn bộ chuỗi Builder v2 + TC/TA **chuyển thẳng**.
   - **Lớp open-state** (quan hệ + xưng hô động, giọng nhân vật, sự kiện, motif, POV):
     phần MỚI thật sự — `entity_relations` 3 tầng + T3/T4 + Narrative Brief.
3. **Đo trước khi tin**: mọi nâng cấp là giả thuyết phải đo (bài học xuyên suốt D2L —
   Auditor, §29, §36 đều đo trước khi wire). Metric mới (ECS/ACS) phải có gold + probe
   riêng trước khi thành headline.
4. **Code không làm việc ngôn ngữ** — mọi phán đoán tự sự (ai nói với ai, pha quan hệ
   nào, giọng gì) thuộc LLM prompt; code chỉ cơ học/tất định
   (memory: code-never-does-language-work).
5. **Không chạy vội lấy số** (LOCK (ll)): trước mọi run tốn API phải render prompt thật
   + preflight cost cho user duyệt (HYG_01 đã dựng pattern này cho TI).
6. **D2L-intact guarantee** (user chốt 2026-07-08): D2L và văn học là **2 nhánh
   profile riêng trên cùng 1 engine** (`technical_d2l_v1` ⟂ `literary_v1` — LOCK (kk)).
   MỌI task literary từ nay mang acceptance bắt buộc: (a) prompt/profile D2L
   byte-identical; (b) test suite D2L xanh; (c) frozen d2l DB hash không đổi.
   Chuyển sang văn học KHÔNG được làm suy suyển nhánh kỹ thuật đã validated.
7. **Truth in labeling** (CodeX review 2026-07-08, Claude verify + đồng thuận):
   D2L chứng minh **nền máy móc** (orchestrator, Builder chain, scoring discipline),
   KHÔNG tự động chứng minh **ngữ nghĩa văn học** (open-state memory, xưng hô, ACS).
   Mọi cơ chế phải mang đúng nhãn trạng thái — xem ma trận §0.1; không dùng bằng
   chứng D2L để over-claim cho literary.

### 0.1 Ma trận bằng chứng — locked / implemented / preflighted / validated

> `locked` = đã quyết trên giấy · `implemented` = có code · `preflighted` = chạy
> offline/0-API có artifact · `validated` = có run thật + số đo được kiểm độc lập.
> Nhãn tại 2026-07-08; L0 có nhiệm vụ điền nốt ô UNKNOWN.

| Cơ chế | Trạng thái | Bằng chứng / ghi chú |
|---|---|---|
| One-button orchestrator + Console + report-full | **validated** (D2L) | run_e79867ab0ec9, E12 verify PASS |
| Builder chain C2→C3→C3.5→C4→C4.5 (term) | **validated** (D2L) | gold metrics, §29/§30/§36 probes |
| Scoring TC/TA surface + cascade localizer | **validated** (D2L) | locate-acc 99.1%; **chưa validate trên văn học** |
| SF-BT / SF-QE / PJ | **validated** (D2L, có caveat) | literary style-judge CHƯA probe |
| Schema T1–T7 + entity_relations | locked + **implemented** (bảng có) | pilot registry HYG đã có 22 glossary / 10 entities / **10 relations** (frozen, `literary_registry_snapshot.json`); full-book/runtime relation population CHƯA validate |
| LiteraryBuilderContextPack | implemented + **preflighted** | HYG_01 audit; **address_policies = 0 → đường xưng hô CHƯA exercise** (verified) |
| Literary translator prompt v2 | implemented + preflighted | **defect đã verify**: profiles.py:32 câu window-only xung đột S1/S3 — sửa §2.4b |
| Injection xưng hô theo pha (as-of) | **locked only** | chưa có code path chạy |
| Chroma 3 collections (store + embed) | locked + implemented + **preflighted** (P4-01 DONE) | `pipeline/retrieval/chroma_store.py`; **runtime hybrid retrieval + D6 CHƯA validate** → probe §4 L3(b) |
| ACS / ECS | **designed only** | probe L3 quyết |
| Gold oracle TI | exists (external), **reference-grade** | nằm NGOÀI runtime repo: `AILAB_HANDOFF/ailab_projects/treasure_island/` — document.json **đã verify 40 units / 1.476 blocks** (Claude 07-08); số 470 terms / 58 entities / 69 relations = LOCK-reported, verify tại path đó trong L1c; thành "gold-grade" CHỈ sau spot-check phân tầng (Q4) |
| Model stack cho ACS/style-judge văn học | pinned default, **chưa probe** | §2.3 |
| Critic Tier 1 rules văn học (nhất-quán-danh-tính, CẤM surface-match) | **locked only** (LOCK §2, PROMPT_DESIGN §1.6) | rules cụ thể chưa spec/chưa code; gate ở L4 |
| **Narrative Agent (A2)** + Interpretation Brief | **locked only** (LOCK §2.1) | = lõi S3; build ở L4, sau gate D6 |
| **Critic Tier 2 (A4)** + Repair loop | **locked only** (LOCK §2.1 state machine) | chưa có trong one-button hiện tại; vào L4 cùng S3 (đúng LOCK §9 P4) |
| POV-switch (chương tự sự XVI–XVIII = units ch20–ch22) | corpus fact, **chưa có case kiểm** | held-out smoke ở L5 |

---

## 1. GIỮ NGUYÊN — kế thừa as-is, không bàn lại

| Mục | Nguồn chốt | Ghi chú |
|---|---|---|
| Ràng buộc GVHD: pipeline tự động từ 0; gold EVAL-ONLY; freeze pre-pass; số liệu bắt buộc | LOCK §0 | bất biến |
| SQLite schema T1–T7 + `entity_relations` (state_label, valid_from/to_block, address_policy_json 2 chiều, precedence pronoun_hints > active state > default > pronoun_policy > style) | LOCK §3 | **đây chính là "các trường văn chương"** — thiết kế đã đủ, chưa cần thêm bảng |
| FREEZE T1–T4 sau pre-pass; runtime chỉ ghi runs/packs/qa/TM | LOCK §3.3 | Builder v2 D2L cũng đã tôn trọng freeze |
| ChromaDB 3 collections (`similar_passages` / `narrative_motifs` / `translation_memory` — embed EN làm khóa, VI payload, chỉ bản pass Critic) | LOCK §4.1 | |
| **Embedding runtime vector DB = `text-embedding-3-large`** (tạo vector context: passages/motifs/TM) | LOCK §4.2 + user re-confirm 2026-07-07 | **PHÂN TẦNG RÕ (tránh lẫn):** bge-m3 local CHỈ thuộc **tầng ĐO** (localizer cascade tier-1, EV-09) — không thay embedding runtime. Hai tầng, hai model, hai mục đích. |
| Đơn vị dịch = WINDOW (4–6 block, biên chương, cắt tại trigger đổi pha relation) + 3 zones cache + inject anchor-based | LOCK §5 | zone budget giữ; pilot TI calibrate lại số |
| Window CẮT tại block trigger đổi pha `entity_relations` (mỗi window 1 state active/entity) | LOCK §5 | luật này là khớp nối then chốt window↔xưng hô |
| 4 LLM agents (World Builder / Narrative / Translator / Critic T2); LLM không chạm DB; Deterministic Context Feeding; không ReAct runtime | LOCK §2.1 | |
| Corpus: ~~Treasure Island vô điều kiện~~ → **⚠ SUPERSEDED bởi Q7 (07-08 (j))**: corpus văn học = cuốn CÓ bản dịch VN đầy đủ, quyết bằng cây quyết định Q7 (TI chỉ giữ nếu tìm được edition "Đảo giấu vàng" đầy đủ). Oracle TI (1.476 blocks verified; 470/58/69 LOCK-reported, tại `AILAB_HANDOFF/ailab_projects/treasure_island/`) giữ nguyên giá trị NẾU TI được giữ; ngược lại archive + annotate lại trên cuốn mới | LOCK §6.1 + §6.3 + Q7 | lý do chọn TI cũ (arc quan hệ, alias, POV) trở thành TIÊU CHÍ BẮT BUỘC cho cuốn thay thế |

**QUY ƯỚC SỐ CHƯƠNG (bẫy đã verify 07-08):** nguồn TI ingest có **40 units =
34 chương tự sự (I–XXXIV) + 6 heading "Part I–VI"** chen ở unit ch01/ch08/ch15/
ch19/ch26/ch33. Hệ quả: **unit-id ≠ số chương tự sự** — vd unit `ch08` là heading
Part II, KHÔNG phải chương VIII "gặp Silver" (chương VIII = unit **ch10**; XXVIII =
unit **ch34**; POV-switch XVI–XVIII = units **ch20–ch22**; smoke ch02/ch03 = chương
I–II, may mắn trùng). **Mọi tham chiếu chương trong doc này là SỐ TỰ SỰ**; L1a phải
xuất bảng mapping tự-sự ↔ unit-id và mọi task/config từ đó dùng unit-id tường minh.
| Human eval E5 bắt buộc cho văn học; judge cross-provider (Gemini); pairwise đảo vị trí 2 lần; calibrate Spearman ρ | LOCK §6.2 | |
| Scope đã loại (LLM coordinator, agentic RAG, graph DB, surface-match check, đếm câu EN=VI…) | LOCK §7 | đừng mở lại |
| Quy trình task: Claude spec → CodeX imple → Claude verify (gate không delegate) | LOCK §8.2 + memory | |

---

## 2. NÂNG CẤP — từng mục lock cũ × bài học D2L

### 2.1 Builder chain: World Builder 1-pass → **chuỗi Builder v2 đầy đủ** ✅ CHỐT 2026-07-08

**Lock cũ:** World Builder đọc tuần tự theo chương + registry-so-far nén + 1 call
Consolidation cuối (LOCK §2.1).

**D2L đã dạy (bằng chứng):**
- Extraction thô over-extract (~46% single-occurrence) → cần **Auditor 2-stage**
  (recall extract → precision critic) — đo được precision↑, recall ≥ floor.
- First-write-wins đóng băng lỗi sớm → cần **re-election cuối pass** (weighted ledger,
  3 gates) + **de-collision CHỈ ledger-backed** (blind de-collision từng phá gold).
- Consolidation code từng bỏ qua `bad_existing_target` → cần ledger-grounded.
- Builder **over-merge head-noun tần suất cao** (model nuốt neural network) — văn học
  rủi ro tương tự với alias ("Silver" nuốt "Long John Silver"? "Captain" nuốt
  "Captain Flint"×2?) → Auditor phải giữ nguyên tắc tách entity theo DANH TÍNH,
  không theo surface.
- Registry-so-far phải **filtered/bounded theo window**, không dump
  (HYG_01 `LiteraryBuilderContextPack` ĐÃ implement đúng hướng này cho TI).
- Injection gate theo `injection_action` 3 mode (hard/soft/excluded), quarantine
  thay drop (§30).

**Đề xuất:** Literary Builder = **kế thừa nguyên chuỗi C2→C3(Auditor)→C3.5(de-collision
gated)→C4(re-election)→C4.5(injection gate)** của D2L, mở rộng OUTPUT từ T1-only sang
T1+T2+T3+T4 + `entity_relations`. Điểm mới cần thiết kế riêng (không có analog D2L):
- **Relation/pha extraction**: Builder đề xuất `state_label` + `valid_from_block` +
  `address_policy_json`; ledger evidence = trích câu thoại. **Re-election cho pha
  KHÔNG bê nguyên D2L** — majority vote sẽ SAI vì pha có thời gian (thù → nghi ngờ
  → đồng minh → phản bội: pha sau không "thắng phiếu" pha trước, nó KẾ TIẾP pha
  trước; §36 voter-model limit đã cảnh báo). Thay bằng **timeline reconciliation**
  tất định: (a) interval không chồng nhau per cặp NV; (b) mỗi chuyển pha phải có
  trigger evidence (trích thoại/sự kiện); (c) merge pha liền kề cùng label;
  (d) giữ nguyên thứ tự thời gian — không re-order theo tần suất.
- **Auditor văn học**: tiêu chí precision khác D2L (termhood → "đáng là entity/motif
  riêng không"), prompt mới, vẫn 2-stage.

**Đo bằng gì:** recall/precision vs gold AI-LAB TI (whole-book: 470 terms /
58 entities / 69 relations — khi đo theo chương phải **slice gold theo đúng chương
pilot**, scope-đo = scope-chạy). Đo trên chương tự sự **VIII + XXVIII** (= units
ch10 + ch34, xem quy ước số chương §1; relation-heavy, thuộc L3 của roadmap §4 —
KHÔNG phải smoke ch02/ch03; smoke chỉ cần dây chuyền sống). Đây chính là
"prelim MLP recall 0.630" của văn học.

### 2.2 Thang điểm: TAR/ECS/ACS ↔ stack đã xây ✅ CHỐT 2026-07-08 (ACS = BẮT BUỘC, user xác nhận riêng)

**Lock cũ (§6.2):** 4 trục — TAR (đúng từ), COMET-Kiwi/COMET/BT (đúng nghĩa),
MQM-lite + ACS (đúng mạch), pairwise + GEMBA + MATTR + human (đúng giọng).

**D2L đã dạy:**
- TAR bản chất = TC/TA đã xây (block_surface / joint_count) — nhưng đã biết đó là
  **surface-proxy có artifact** (any-match inflation ~20%, nesting/leakage).
  Bản chuẩn = **localizer cascade** (bge-m3 region → code rules → Gemma T3 local →
  human), locate-acc 99.1%, T3 $0.
- **Scope-đo = scope-dịch** (validity bug nếu lệch); **không tune can thiệp trên
  test** (S1 terms phải lock a-priori/dev); gold văn học = style-guide người →
  strict-metric artifact (vectơ-vs-vector) → luôn giữ diagnostic bên cạnh headline.
- SF-BT (cos + LLM) / SF-QE (CometKiwi, caveat vi zero-shot CPU) / PJ (order-swap,
  order_inconsistent phải báo cáo) đã chạy thực chiến trong one-button.

**Đề xuất map (văn học):**
| Trục lock cũ | Hiện thân đã có | Việc mới |
|---|---|---|
| TAR (đúng từ) | TC/TA + cascade localizer | đổi nội dung registry: tên NV + alias + T1 hàng hải; **xây ECS/ACS trên nền cascade ngay từ đầu**, không đi lại vòng surface-proxy |
| ECS (entity consistency) | CHƯA có — công thức entropy đã chốt | implement trên occurrence do cascade định vị; alias `aliases_target` không phạt |
| **ACS (xưng hô theo pha)** — lock cũ để "optional 🔶" | CHƯA có | **NÂNG CẤP THÀNH BẮT BUỘC** — đây là metric chứng minh giá trị lớp open-state (đóng góp chính); extract cặp xưng–hô VI (dict + LLM-extract như T3 locator pattern) → so `address_policy` active theo pha. Cần probe kiểu EV-09 trước khi thành headline. |
| Đúng nghĩa | SF-BT/SF-QE nguyên trạng | caveat giữ |
| Đúng giọng | PJ nguyên trạng + GEMBA/MATTR/human E5 | pairwise là trục CHỦ LỰC văn học (ref-based chỉ phụ — lock đã nói) |

**Nguyên tắc mới ghi thẳng vào thiết kế metric:** mọi metric văn học khai báo
method + caveat ngay từ đầu (bài học E12 — nhãn trung thực là first-class).

### 2.3 Model stack: chốt mềm → **pinned default, subject to literary probe** ✅ CHỐT 2026-07-08

> REVISE 2026-07-08 (CodeX review, Claude đồng thuận): D2L chứng minh **hạ tầng**
> chạy được với stack này, KHÔNG chứng minh Gemma đủ tốt cho ACS-extract văn học
> hay Gemini đủ tốt làm literary style judge. Nhãn đúng = "pinned default" (không
> đổi khi chưa có lý do), việc chốt cứng cho từng vai literary đi qua probe L3.

**Lock cũ (§2.2, 🔶):** gpt-5.4-mini toàn pipeline; Gemini judge/BT;
text-embedding-3-large.

**D2L xác nhận bằng số thật (E12 report-full, run_e79867ab0ec9):**
- gpt-5.4-mini: Builder+Translator ổn định, JSON fail thấp, real cost cực rẻ
  (cả run S0+S1 D2L preface: **$0.248 thật** vs cap $0.688).
- gemini-2.5-flash: judge SF-BT/PJ chạy ổn (caveat order_inconsistent 15/30 phải
  báo cáo, đã có nhãn).
- **gemma-4-12b local**: T3 locator 103/103 (thắng GPT 101/103), $0,
  repeat_penalty=1.0 bắt buộc — vai trò TẦNG ĐO.
- **bge-m3 local**: tier-1 region assignment (sentence-hit ~0.98) — vai trò TẦNG ĐO.
- reasoning_effort ăn output budget (~16× cost, empty response ở cap thấp) → giữ
  none/minimal cho locator + translator như đã chốt.

**Đề xuất:** PIN bảng vai trò 2 TẦNG làm default (vai đánh dấu ⚠ = phải qua probe
L3 trước khi coi là chốt cho literary):
| Tầng | Model | Việc |
|---|---|---|
| **DỊCH (runtime)** | gpt-5.4-mini (pin) | World Builder, Narrative, Translator, Critic T2 |
| **DỊCH (runtime)** | text-embedding-3-large | vector hóa context: similar_passages, narrative_motifs, TM (LOCK §4.2) |
| **ĐO (eval)** | gemini-2.5-flash | judge MQM-lite/PJ/SF-BT-llm, BT VI→EN — ⚠ literary style-judge chưa probe |
| **ĐO (eval)** | bge-m3 local | cascade tier-1 region cho TC/TA/ECS/ACS |
| **ĐO (eval)** | gemma-4-12b local | cascade T3 locator; ⚠ ACS extract = ỨNG VIÊN, probe L3 quyết (Q2) |
| **ĐO (eval)** | CometKiwi local | SF-QE |

### 2.4 Injection/pack văn học ✅ CHỐT 2026-07-08

**Lock cũ:** zones + anchor-based + hard/soft split (LOCK §5, RETRIEVAL doc).

**D2L đã dạy:** pack chỉ đưa **MỘT form/term** (variant list làm nhiễu — 62/62 prompt
verified); soft-tier "deviation rights" không hại consistency (soft group tốt nhất);
consistency đến từ **anchor ổn định chia sẻ**, không phải coordination giữa window;
không mid-run write-back; min_occ=0 CHỈ an toàn sau Auditor gate.

**Đề xuất cho pack văn học (mở rộng, không thay):**
- Term/tên NV: one-form anchor như D2L (canonical duy nhất trong pack).
- **Xưng hô**: pack bơm `address_policy` của **pha ACTIVE tại window đó** (một cặp
  self/address duy nhất cho mỗi cặp NV có mặt) — analog trực tiếp của one-form
  anchor nhưng **index theo (cặp NV, pha)**. Window đã cắt tại trigger đổi pha nên
  mỗi window chỉ 1 policy/cặp — thiết kế lock cũ + bài học D2L khớp nhau đẹp ở đây.
- Character card: chỉ NV có mention trong window (anchor-based như cũ) + narrator
  card luôn có nếu first-person (HYG_01 đã chốt).
- Giữ `context_audit` (included/excluded/matched_by/dropped_by_budget) như HYG_01
  + memory_packs.token_breakdown — Console/Cockpit đọc được ngay.
- **Bất biến "as-of" (ghi rõ, từ nghiên cứu 07-08 §6.2):** relations/pha inject theo
  trạng thái ACTIVE tại block hiện tại (`valid_from ≤ block < valid_to`) — không bơm
  pha tương lai. Ngược lại, synopsis/motif whole-book trong Zone 1 là CHỦ ĐÍCH giữ
  (dịch giả người cũng đọc hết sách trước khi dịch); chỉ XƯNG HÔ/QUAN HỆ phải đúng
  thì-hiện-tại của truyện.

**2.4b — DEFECT đã verify, sửa BẮT BUỘC trước mọi run S1 literary:**
`pipeline/translate/profiles.py:32` (LITERARY_SYSTEM_PROMPT) hiện ghi *"You see only
this window — consistency beyond the window is not your concern."* — đúng cho S0
(không memory), nhưng **mâu thuẫn trực diện** với S1/S3 khi pack cấp address_policy /
term anchor: câu này cho Translator quyền bỏ qua chính thứ memory bơm vào. Kèm theo:
HYG_01 audit ghi `address_policies: 0` → toàn bộ đường xưng hô chưa từng có mặt
trong prompt thật nào. **Fix:** tách câu theo mode — S0 giữ nguyên; S1/S3 (wording
REVISE 07-08 theo CodeX: bản đầu dùng "OVERRIDES" chung cho cả terminology có rủi
ro ép verbatim qua cửa sau — vi phạm "hard ≠ verbatim" LOCK/PROMPT_DESIGN §1.6):
*"If an ADDRESS POLICY is provided, follow its relationship-level self/address
terms for this window. If a terminology/name anchor is provided, preserve the
specified identity/term choice while still writing natural Vietnamese; do not
copy wording mechanically."*
→ bump `literary_translator_v3` (prompt_version vào cache key như HYG_01 đã làm).

### 2.5 Hạ tầng one-button/Console: D2L để lại, văn học hưởng trọn ✅ CHỐT 2026-07-08

**Lock cũ chưa có khái niệm này** (viết trước khi one-button ra đời).

**Đề xuất:** thêm profile `literary_v1` vào one-button (stages: ingest? →
builder_chain → translate S0/S1[/S3] → score_run → sf_qe/sf_bt/pj → final);
Console + report-full (E12) dùng nguyên — chỉ cần scores table nhận thêm
ECS/ACS rows. Frozen-DB discipline + workdb `_work/` + estimate/preflight
giữ nguyên. **KHÔNG viết orchestrator mới.**

### 2.6 Bốn tầng agent đã chốt — vị trí từng tầng trong lộ trình ✅ CHỐT 2026-07-08

**Nhắc lại từ LOCK §2.1 (tránh hiểu nhầm "chỉ có Builder + Translator"):** thesis
khóa đúng **4 LLM agents** — A1 World Builder (pre-pass), A2 Narrative
Understanding (S3, Interpretation Brief), A3 Translator, A4 Critic Tier 2 (+ vai
ẩn Repair/Consolidation/Judge). **KHÔNG theo mô hình công ty 6-role của
TransAgents** (LOCK §1 định vị khoảng trống, §7 loại LLM-coordinator) —
TransAgents (TACL 2025, `reference/papers/2025.tacl-1.42.pdf`) là related-work
để differentiate, không phải template.

| Agent | D2L đã chạy? | Văn học vào bước |
|---|---|---|
| A1 Builder (+Auditor LLM trong chuỗi) | ✅ | **L2a Builder pilot** (đi trước dịch, như D2L) |
| A3 Translator | ✅ | L2b smoke |
| A2 Narrative (Brief) | ❌ | L4, sau gate D6 (retrieval sống mới đáng nén brief) |
| A4 Critic T2 + Repair (+ Critic T1 code rules văn học) | ❌ | L4 cùng đợt S3 (đúng LOCK §9 P4); T1 rules spec ở L2b để chạy sớm dạng report-only |

---

## 3. CẦN CHỐT — câu hỏi mở (user / GVHD)

| # | Câu hỏi | Phương án | Ai chốt |
|---|---|---|---|
| Q1 | S3 (Narrative Brief) vào pilot văn học ngay, hay pilot S0/S1 trước rồi thêm S3? | **✅ CHỐT 07-08: giữ roadmap, NÉN LỊCH SONG SONG** — L2 smoke S0/S1 (LƯU Ý đã làm rõ với user: S1 văn học ĐÃ chứa address_policy/xưng hô theo pha — hard constraint, không phải "chỉ từ điển"); L3 probe chạy song song L2; S3d/S3 vào L4 ngay khi D6+ACS pass. User yêu cầu tiến độ gấp — ưu tiên parallel hóa, không bỏ gate | ✅ |
| Q2 | ACS: extract cặp xưng-hô bằng gì? | (a) dict đại từ + regex trước, LLM chỉ ca khó (rẻ, theo tinh thần cascade); (b) LLM-extract toàn bộ (gemma local $0) | probe quyết |
| Q3 | Dual-track trình GVHD: xác nhận giữ TI song song D2L (LOCK §8 mục 3b còn nợ) | gộp vào buổi báo cáo pilot | user + GVHD |
| Q4 | Gold AI-LAB TI (oracle CodeX): dùng trực tiếp làm eval gold? | REVISE 07-08: spot-check **PHÂN TẦNG theo lớp** (terms / entities / aliases / relations / address_policy / speaker_turns / motif — mỗi lớp n nhỏ riêng), KHÔNG random block chung — vì ECS/ACS ăn vào lớp relations/address là lớp oracle dễ sai nhất. Trước spot-check: oracle = reference-grade only | user |
| Q5 | Pilot chapters | **ĐÃ CHỐT 07-08 (CodeX + Claude hội tụ): 2 pilot tách vai** — (a) SMOKE units ch02/ch03 = chương tự sự I–II (đã có ingest + prompt artifact HYG_01, mục tiêu = dây chuyền sống end-to-end); (b) RELATION/ACS probe chương tự sự VIII + XXVIII = **units ch10 + ch34** (tối thiểu; mở rộng XI/XIV–XV = units ch13/ch17–ch18 nếu cần) — chỉ smoke KHÔNG đủ kiểm chứng văn học | ✅ |
| Q6 | Chuỗi Builder v2 áp cho T2/T3/T4: Auditor văn học 1 prompt chung hay tách theo tier (entity vs motif)? | thiết kế ở task spec, đo pilot | Claude spec → đo |
| Q7 | **CORPUS VĂN HỌC DUY NHẤT — quyết bằng cây quyết định (user chốt 07-08):** nghiên cứu BẮT BUỘC có bản dịch người đầy đủ để so ("production không có reference, nhưng thí nghiệm phải có — nếu chạy trên cuốn không gold người thì sau phải chạy lại cuốn khác = tốn thời gian + tiền"). Cây quyết định: (1) nếu "Đảo giấu vàng" CÓ edition dịch đầy đủ → **GIỮ TI** (mọi tài sản oracle nguyên vẹn + thêm track ref-based); (2) nếu KHÔNG → **CHUYỂN corpus** sang cuốn Q7 tốt nhất — TI archive, oracle annotation LÀM LẠI trên cuốn mới (pattern/code giữ nguyên, chỉ data làm lại; ~1,9M tok quota + thời gian), tiêu chí (d) giàu thoại/arc quan hệ trở thành BẮT BUỘC (cuốn duy nhất phải gánh cả vai memory-showcase), memorization test thành bắt buộc-sớm (bản dịch nổi tiếng → leakage cao). Cách dùng reference (LOCK §6.2): COMET ref-based chính; chrF/d-BLEU phụ + caveat TransAgents; so định tính; **EVAL-ONLY tuyệt đối**. Cần bước ALIGN bản dịch người về block (spec sau khi chọn cuốn) | CodeX research (ĐANG CHẶN đường găng L1a/c/d) → user + Claude chốt | user |

---

## 4. Lộ trình L0–L5 (map vào P2–P6 của LOCK §9)

> Mỗi bước có gate; không bước nào chạy API khi bước trước chưa qua gate.
> HYG_01 (context pack + prompt render) DONE; HYG_02 (recall density preflight)
> đã có task — L1 xây tiếp trên đó.

> REVISE 2026-07-08 theo CodeX review: tách smoke ⟂ relation-probe; mọi run "lấy số"
> phải **đăng ký trước** (pre-registered: config + metric + split khai báo trước khi
> chạy); thêm retrieval probe (= **D6 của lock**, không phải khái niệm mới) trước S3.
>
> ⚠ **REVISE 07-08 (Q7 chặn corpus):** L1a/L1c/L1d và mọi bước sau nhắm vào corpus
> do cây quyết định Q7 chọn (TI nếu có bản dịch đầy đủ, ngược lại cuốn Q7). Mọi tham
> chiếu "TI / ch10 / ch34 / oracle" trong bảng dưới đọc là "corpus được chọn +
> chương tương đương giàu quan hệ của nó". Chạy được NGAY không cần chờ Q7:
> **L1b (prompt v3), spec Critic T1, task cache-hit metric** — đều corpus-agnostic.

| Bước | Việc | Gate ra | API? |
|---|---|---|---|
| **L0 Evidence + reconcile** | User duyệt doc từng mục `[CHỐT]`; ma trận §0.1 đã hết UNKNOWN (CodeX review 07-08 điền Chroma + Claude verify); ghi changelog vào LOCK | user duyệt | 0 |
| **L1a Pin + split** | Pin TI snapshot; xuất **bảng mapping chương tự-sự ↔ unit-id** (40 = 34 + 6 part-heading, đã verify §1); khai báo DEV/TEST split chương TRƯỚC khi thấy output | mapping table + split khai báo, commit | 0 |
| **L1b Prompt v3** | Fix §2.4b (wording đã chốt); bump `literary_translator_v3` vào cache key; render sample S0 vs S1 cho user duyệt | prompt v3 render sample được duyệt | 0 |
| **L1c Oracle spot-check** | Spot-check PHÂN TẦNG theo lớp (Q4) tại `AILAB_HANDOFF/ailab_projects/treasure_island/`; xác nhận số 470/58/69; **đếm speaker_turns theo chương/cặp NV** (input cho L3a denominator) | gold từng lớp có verdict; bảng đếm speaker_turns | 0 (human) |
| **L1d Relation/address preflight** | Builder literary preflight mở rộng trên chương giàu relation (unit theo mapping L1a): relations/address CÓ MẶT THẬT trong pack — hết `address_policies=0` | render sample có ADDRESS POLICY thật, context_audit đếm được | 0 |
| **L2a BUILDER pilot (đi trước dịch — user chốt 07-08, như lộ trình D2L)** | Chạy chuỗi Builder literary (C2→Auditor→…→C4.5) trên chương DEV: T1 + entities + relations/pha + address_policy; **đo recall/precision vs oracle slice** (unit ch10/ch34 relation-heavy + ch02/ch03); FREEZE memory DB; **index Chroma**: `similar_passages` (từ source EN — có thể index ngay sau L1a) + `narrative_motifs` (từ T4 Builder vừa xây); render pack mẫu | notebook văn học sống: relations/address_policy có nội dung thật; recall/precision có số (floor chốt sau prelim); JSON fail <5%; **memory.sqlite frozen + 2 collection Chroma đã index** (nguyên liệu cho D6 probe L3b và S3 L4) | API nhỏ (preflight trước) |
| **L2b SMOKE dịch** | S0 + S1 trên **ch02/ch03** (one-button profile literary) dùng notebook L2a; spec Critic T1 rules văn học chạy dạng report-only; mục tiêu = dây chuyền sống end-to-end, KHÔNG phải số đẹp | 2 chương dịch trọn, report-full đủ thang, không stage gãy; **= mốc P3 "dịch được"** | có (preflight trước) |
| **L3 Probe kép (relation-heavy)** | (a) **ECS/ACS probe** trên chương tự sự **VIII + XXVIII** (= units ch10 + ch34 theo mapping L1a; Jim↔Silver, pha + xưng hô; **denominator n lấy từ bảng đếm speaker_turns L1c — KHÔNG cam kết cứng n~100 trước khi đếm**; nếu thoại gán speaker rõ quá mỏng → ACS thu về subset thoại trực tiếp có speaker evidence, phần còn lại qua MQM-lite/human như §5; dict-vs-LLM theo Q2); (b) **Retrieval probe = D6** (relevance set nhỏ: chapter summary / motif / relation / TM; Recall@k, MRR — 3 cửa lock (j): hard ≥99%, low_context <5%, D6 Recall@5) | ACS extract-acc có số thật trên denominator đã đếm; D6 qua 3 cửa — **fail cửa nào thì S3 chưa được chạy** | local $0 + ít |
| **L4 Pilot ĐĂNG KÝ TRƯỚC** | S0/S1/S3d/S3 trên chương DEV đã khai báo — đợt này build A2 Narrative (Brief) + A4 Critic T2 + Repair (đúng LOCK §9 P4, xem §2.6); config + metric + floor viết ra TRƯỚC run; CHỈ chạy sau khi L3 (a)+(b) pass | số S0-vs-S1-vs-S3 hợp lệ (không tune-on-test); Critic/Repair có mặt trong state machine | có |
| **L5 Held-out + báo cáo** | Chấm trên chương held-out (TEST split L1a) — **không tuning lại bất kỳ policy nào**; **+ POV-switch smoke** (chương tự sự XVI–XVIII = units ch20–ch22, kiểm narrator đổi không gãy xưng hô/POV); case study Jim↔Silver đổi pha (định tính Chương 4); báo cáo GVHD 2 trang + Q3 dual-track | số held-out + POV smoke + case study; go/no-go scale nguyên cuốn (P6) | có |

**Đường găng:** L0→L1a–d→**L2a (Builder trước)**→L2b→L3→L4→L5 (L1b/L1c/L1d song
song sau L1a; L3 có thể song song L2b vì probe ăn output Builder L2a). Mọi task
giao CodeX theo template, Claude verify trên artifact thật.

---

## 5. Rủi ro chính & phòng thủ

| Rủi ro | Phòng thủ |
|---|---|
| **Tầng đo ACS chưa từng tồn tại** — nếu ACS không extract nổi cặp xưng-hô đáng tin thì claim trung tâm mất máy đo | L3 probe TRƯỚC L4; nếu extract-acc thấp → thu hẹp ACS về subset thoại trực tiếp có speaker_turns rõ, phần còn lại qua MQM-lite discourse + human |
| Builder văn học recall thấp trên open-state (relation/pha khó hơn term) | pilot DEV + floor riêng; ledger evidence bắt buộc trích thoại; fallback = giảm scope pha về 3 mốc lớn của arc Jim↔Silver |
| TI leakage (nổi tiếng, có bản dịch VN) | như D2L: ablation delta + memorization test (dịch ~10 đoạn so verbatim) — LOCK đã chốt |
| Gold oracle = AI-sinh | Q4 human spot-check trước khi làm denominator; tách `_internal` vs `_gold` như lock §6.2 |
| Tiếng Anh TK19 + khẩu ngữ thủy thủ làm SF-QE (vi zero-shot) nhiễu thêm | SF-QE giữ vai directional; trục chủ lực = pairwise + human (lock đã định) |
| Scope phình (34 chương × full ladder) | pilot 2 chương DEV trước mọi quyết định scale; quota ngày đã có throttle/checkpoint |

---

## 6. Chắt lọc nghiên cứu ngoài (GPT + Gemini repo surveys, 2026-07-08)

> Nguồn: `research/nghiên cứu tham khảo các repo - gpt.md` + `- gemini.md`.
> Nguyên tắc lọc: chỉ giữ thứ (a) bổ sung được vào mục §1–§5, hoặc (b) là đạn
> phòng thủ/related-work cho luận văn. Thứ mâu thuẫn với LOCK §7 ghi rõ REJECT + lý do.

### 6.1 Verdict RAG — GraphRAG vs Classic vs Agentic ✅ CHỐT 2026-07-08 (không đổi thiết kế — duyệt cùng gói §2)

**Kết luận: thiết kế hiện tại ĐÃ LÀ GraphRAG về data model; không mở lại machinery.**

| Ứng viên | Verdict | Căn cứ |
|---|---|---|
| Classic/naive RAG (vector-only) | REJECT làm cơ chế chính (đã loại từ đầu) | RETRIEVAL doc §A: thiếu context văn học, sai xưng hô; vector CHỈ cho soft track |
| Agentic RAG (LLM tự query, lặp tự do) | REJECT (LOCK §7 giữ nguyên) | Cả 2 survey đồng thuận; số Briva-Iglesias 2025: iterative agent ~**15×** token, sequential ~**5×** — selective activation là bắt buộc |
| **GraphRAG (tư duy đồ thị)** | **ĐÃ CÓ** — `entity_relations` + valid_from/to + evidence = temporal knowledge graph trong SQLite; "GraphRAG-shaped offline, keyed-lookup online" (LOCK changelog (i)) | Cả 2 survey hội tụ về đúng thiết kế này: GPT-survey "giữ graph ở mức schema và retrieval logic, chưa cần graph DB chuyên dụng"; Gemini-survey đề cao KG cho quan hệ NV nhưng bộ máy Neo4j/community-detection/multi-hop = quá mức cho 1-hop keyed lookup |
| Graph DB machinery (Neo4j, GraphRAG indexing, community summaries) | REJECT (LOCK §7) — indexing đắt (chính docs GraphRAG cảnh báo), thêm moving part không phục vụ RQ | |

**Giá trị mới thu được:** tên gọi + citation để ĐỊNH VỊ memory trong Chương 2/3:
memory của thesis thuộc họ **temporal-KG dual-layer** — cùng họ **Graphiti** (validity
windows + provenance = đúng `valid_from/to_block` + evidence_json đã chốt),
**LightRAG** (dual-layer graph+vector = SQLite+Chroma), **GraphRAG** (offline
graph extraction mindset = pre-pass World Builder). "Diegetic time injection"
(Gemini-survey) = bất biến as-of đã ghi vào §2.4.

### 6.2 Database — đối chiếu "narrative OS checklist" (GPT-survey)

Checklist bảng mà GPT-survey khuyên ≈ **khớp 100% schema đã chốt** (blocks/chapters ✓,
entities+aliases ✓, relations+pha+evidence ✓, glossary hard/soft/preserve ✓ =
injection_action §30, chapter_summaries/motifs ✓ = T4, TM ✓, audit
memory_packs/translation_runs/evaluation_runs/qa_issues ✓). Kết luận: **schema không
thiếu bảng nào** — khoảng trống chỉ ở NỘI DUNG (Builder văn học phải điền T2/T3/T4
thật, §2.1) chứ không ở cấu trúc.

- **Postgres+pgvector / Qdrant / Milvus** (GPT-survey khuyên): **REJECT cho thesis** —
  SQLite+Chroma đã khóa; ít moving part = dễ tái lập, đúng lập luận chính survey đó
  cũng thừa nhận ("càng ít moving parts vô ích càng dễ chứng minh cải thiện do
  kiến trúc"). Ghi nhận làm phương án scale-out TƯƠNG LAI nếu vượt 1 máy.
- **RAPTOR** (cây tóm tắt block→scene→chapter→arc): ý tưởng đúng hướng cho T4;
  V1 giữ `scenes/events` trống như lock; đánh dấu **V2-candidate** cho hierarchical
  summary nếu chapter summary phẳng tỏ ra thiếu (đo bằng low_context rate).
- **CRAG** (retrieval evaluator + corrective re-retrieve): = Coverage Checker +
  re-retrieve-1-lần đã chốt — thêm citation, không thêm việc.

### 6.3 Context cache — cơ chế hoạt động & hệ quả thiết kế (câu hỏi trọng tâm user)

**Cơ chế (tổng hợp 2 survey + provider docs):** provider lưu **KV/attention state
của PREFIX prompt**; request sau khớp prefix **byte-identical** thì bỏ qua prefill
phần đó. OpenAI: automatic, ngưỡng **≥1024 tok**, đọc cache ≈ 10% giá (đã là nền
Zone-design LOCK §5.1). Anthropic: ghi cache +25%, đọc −90%, TTL 5 phút. Gemini:
đặt nội dung chung lên ĐẦU, gửi request cùng prefix gần nhau, soi hit qua
`usage_metadata`. **1 token khác ở đầu = mất toàn bộ cache phía sau** → mọi thứ
động (timestamp, run-id) phải nằm CUỐI prompt.

**Đối chiếu:** layout 3-zone + kỷ luật byte-identical + "Zone 1 giàu-mà-ổn-định rẻ
hơn gọn-mà-uncached ~5×" (LOCK §5.1) = đúng best practice cả 2 survey mô tả
(volatility-ascending: tools → system → reference → history → query). **Không đổi
thiết kế.**

**Bổ sung ĐO ĐƯỢC (mới, từ E12):** `usage_json` mỗi call đã lưu **`cached_tokens`**
(đã verify trên translate_cache run D2L — hiện =0 vì call ít, chưa chung prefix đủ
1024). → Khi chạy literary với Zone 1 tĩnh: **dự đoán kiểm chứng được** =
`cached_tokens/prompt_tokens` phải tăng rõ từ window thứ 2 cùng chương. Đưa
**cache-hit-rate thành metric hạ tầng** trong report-full/Chương 4 (bằng chứng
cost-engineering của zone design — số thật, không phải lý thuyết). ✅ CHỐT 07-08: LÀM.

- vLLM (PagedAttention/chunked prefill/prefix caching): chỉ liên quan nhánh
  local-serving (LM Studio hiện tại đủ cho Gemma/bge tầng đo) — tham khảo, không việc.
- GPTCache semantic cache: REJECT (survey tự nhận đã ngừng cập nhật 2024; replay
  cache theo (model,prompt_hash,temp,seed) đã có và đúng nhu cầu hơn).
- "Lost in the Middle": citation củng cố luận điểm online-path-gọn O(1) (LOCK §5.1).

### 6.4 Related work + eval bổ sung

- **DelTA** (multi-level memory: proper-noun records / bilingual summary / LT / ST
  memory) = **họ hàng gần nhất** với taxonomy memory của thesis; khác biệt để viết
  rõ: DelTA cập-nhật-online tuần tự, thesis = **pre-pass FREEZE + window + infra
  tất định** (tái lập + ablation sạch). Metric **LTCR-1** của DelTA = tiền lệ
  consistency-metric cho TC lineage. `[thêm vào RELATED_WORK]`
- **Sent2Sent++** (forced decoding chống sót câu) = giải cùng bài toán với Coverage
  Checker + META uncertain_spans — citation đối chiếu.
- **TransAgents / translation-agent (Andrew Ng) / Aphra**: đã có trong lock/V3;
  survey xác nhận vị trí (TransAgents = related-work bắt buộc; Ng = baseline
  reflection S1/S2-analog; Aphra = glossary+critique pipeline nhỏ).
- **DITING 6 chiều** (idiom / lexical ambiguity / terminology localization / tense
  consistency / **zero-pronoun** / cultural safety): khung xây **challenge-set EN–VI**
  cho TI — đặc biệt **zero-pronoun/referent recovery** vì tiếng Việt lược chủ ngữ
  dày đặc, chạm thẳng bài toán xưng hô (ACS §2.2). ✅ CHỐT 07-08: ĐỂ SAU L5
  (user: làm A cache-metric trước, nâng cấp lên B sau khi pipeline ổn định).
- **DocCOMET / XCOMET** (document-level, error spans): ứng viên nâng cấp trục
  "đúng nghĩa" khi lên full run — ghi nhận, chưa cam kết.
- **"Memory health" mindset** (LoCoMo/LongMemEval): đo memory subsystem ĐỘC LẬP với
  translation quality — thesis đã làm đúng hướng này (Builder recall vs gold,
  `_internal` vs `_gold` tách bạch LOCK §6.2); lấy thuật ngữ "memory health" đặt tên
  cho nhóm metric đó trong Chương 4.
- Số chi phí đối trọng cho §5 + Chương 4: multi-agent sequential ~5× / iterative
  ~15× token (Briva-Iglesias & Dogru 2025) → biện minh selective activation +
  deterministic infra của thesis.

## 7. Changelog

- **2026-07-07** — Tạo doc (Claude). Nguồn: LOCK §0–§9 + changelog, HYG_01/02,
  memory D2L (Builder v2, §29/§30/§36, cascade localizer, EV-09, scoring framework,
  E12 real-cost). User re-confirm: text-embedding-3-large = embedding RUNTIME vector
  DB; bge-m3 local = tầng ĐO only.
- **2026-07-08** — Thêm §6 chắt lọc 2 survey ngoài (GPT/Gemini). Verdict: GraphRAG
  data-model ĐÃ CÓ trong thiết kế (entity_relations = temporal KG; họ
  Graphiti/LightRAG), machinery vẫn REJECT theo LOCK §7; agentic RAG REJECT (5×/15×
  cost). DB checklist khớp 100% schema — thiếu NỘI DUNG không thiếu BẢNG;
  Postgres/Qdrant/Milvus reject cho thesis. Cache: cơ chế prefix-KV + bổ sung
  metric đo được `cached_tokens/prompt_tokens` (cache-hit-rate) vào report-full.
  Bất biến as-of cho relations ghi vào §2.4. Related work mới: DelTA (họ hàng gần
  nhất), Sent2Sent++, DITING zero-pronoun challenge-set candidate.
- **2026-07-08 (b)** — Patch theo CodeX review (Claude verify 2 claim trước khi
  nhận: profiles.py:32 câu window-only CÓ THẬT; HYG_01 audit address_policies=0
  CÓ THẬT). Thêm: nguyên tắc §0.6 truth-in-labeling + **ma trận bằng chứng §0.1**;
  §2.1 re-election pha → timeline reconciliation (4 luật, bỏ majority); §2.3 model
  stack hạ nhãn "pinned default, subject to literary probe"; **§2.4b defect prompt
  bắt buộc sửa** (literary_translator_v3); Q4 spot-check phân tầng theo lớp; Q5
  ĐÃ CHỐT 2-pilot (smoke ch02/03 ⟂ relation ch8+ch28); §4 roadmap L0–L5 viết lại:
  pre-registered runs, retrieval probe = D6 của lock làm gate trước S3, L5 held-out
  không tuning. Điểm Claude chỉnh lại CodeX: "retrieval probe" không phải khái niệm
  mới — là D6 + 3 cửa lock (j), chỉ được xếp lịch cụ thể.
- **2026-07-08 (c)** — Rà cuối (Claude): sửa 7 điểm — §2.3 hết tự-mâu-thuẫn
  ("chốt cứng"→PIN default + đánh dấu ⚠ vai chưa probe); bullet context_audit trả về
  đúng §2.4; §2.1 ghi rõ đo builder trên ch8+ch28 (L3) ≠ smoke ch02/03 + slice gold
  theo chương; Q1 ghi rõ roadmap đã encode phương án (a) chờ user gật; §1 corpus row
  hạ nhãn oracle = reference-grade (khớp §0.1); L1 thêm đối chiếu 40-vs-34 chương
  oracle khi pin snapshot; typo one-button + Q3 "§8 mục 3b".
- **2026-07-08 (d)** — Patch theo CodeX review lượt 2 (verdict NEEDS-REWORK; Claude
  verify các claim chính trước khi nhận — TẤT CẢ ĐÚNG: chroma_store.py tồn tại +
  P4-01 DONE; snapshot 22/10/10; document.json 40 units/1.476 blocks với 6 part-
  heading đúng vị trí ch01/08/15/19/26/33). Sửa: §0.1 Chroma → implemented+
  preflighted (D6 chưa validate); §0.1 relations row → pilot 10 relations frozen;
  thêm 2 row Critic-T1-literary + POV-switch; oracle row + §1 ghi path external
  `AILAB_HANDOFF/ailab_projects/treasure_island/` (470/58/69 = LOCK-reported, verify
  L1c); §2.4b wording bỏ "OVERRIDES" chung — address theo policy, terminology giữ
  danh tính nhưng viết tự nhiên (hard ≠ verbatim); L1 tách L1a–d; L3a denominator
  lấy từ đếm speaker_turns L1c (không cam kết n~100 trước); L5 thêm POV smoke.
  **Phát hiện thêm của Claude khi verify:** unit-id ≠ số chương tự sự (unit ch08 =
  heading Part II, KHÔNG phải chương VIII) → thêm QUY ƯỚC SỐ CHƯƠNG vào §1 +
  quy đổi mọi tham chiếu: probe VIII/XXVIII = units ch10/ch34, POV XVI–XVIII =
  units ch20–ch22; L1a phải xuất bảng mapping.
- **2026-07-08 (e)** — **L0 PASS.** User duyệt qua hỏi-đáp trực tiếp: gói §2.1–§2.5
  cả gói ✅; ACS nâng BẮT BUỘC ✅; Q1 = giữ roadmap + nén lịch song song (sau khi
  làm rõ S1 văn học đã chứa xưng hô/address_policy — không phải "chỉ từ điển";
  user ưu tiên tiến độ gấp) ✅; phụ trợ: cache-hit metric LÀM ngay, DITING
  challenge-set để sau L5 ✅. §6.1 duyệt cùng gói (không đổi thiết kế).
  Doc chuyển trạng thái DRAFT → L0-approved; mở đường TASK L1a–d.
- **2026-07-08 (f)** — User bổ sung 2 quyết định: (1) **D2L-intact guarantee**
  thành nguyên tắc §0.6 — 2 nhánh profile riêng trên 1 engine, mọi task literary
  phải chứng minh D2L nguyên vẹn (prompt byte-identical + test xanh + frozen hash);
  (2) mở **Q7**: tìm tiểu thuyết có bản dịch VN xuất bản ĐẦY ĐỦ làm corpus
  cross-check ref-based (hệ dịch 1:1 block → reference bắt buộc unabridged);
  TI giữ vai corpus chính. CodeX nhận research task.
- **2026-07-08 (g)** — User nhắc đúng lỗ hổng: thesis chốt **4 LLM agents**
  (LOCK §2.1) chứ không chỉ Builder+Translator — thêm §2.6 định vị A2 Narrative
  + A4 Critic T2/Repair (cả hai locked-only, vào L4 đúng LOCK §9 P4) + 2 row ma
  trận §0.1; nắn lại: TransAgents (TACL 2025) là related-work để DIFFERENTIATE
  (LOCK §1/§7 loại mô hình 6-role), không phải template. User chốt Builder-first:
  L2 tách **L2a Builder pilot** (đo recall/precision vs oracle trước) → **L2b
  smoke dịch**; Critic T1 rules văn học spec ở L2b dạng report-only.
- **2026-07-08 (h)** — Làm rõ 2 điểm user hỏi: (1) TI bản dịch VN rút gọn KHÔNG
  làm mất vai TI — reference của TI = gold tự xây (LOCK §6.1), bản dịch xuất bản
  chưa bao giờ là chỗ dựa; ghi chú vào Q7. (2) Thứ tự Builder → SQLite → FREEZE →
  index Chroma → RAG pack → Translator là pipeline khóa; bước **index vector DB**
  trước đây bị ẩn — nay ghi tường minh vào L2a (similar_passages sau L1a,
  narrative_motifs sau Builder; gate = memory frozen + 2 collections indexed).
- **2026-07-08 (i)** — User làm rõ nhu cầu Q7: nghiên cứu (khác production) CẦN
  bản dịch người đã kiểm chứng để trả lời "dịch tốt/đúng không" → **nâng vai
  cuốn Q7** từ cross-check phụ thành **corpus đo chất lượng dịch chính** (đối
  xứng d2l-vn). TI giữ vai memory-showcase. Cách dùng reference: COMET ref-based
  chính / chrF-BLEU phụ có caveat / so định tính / EVAL-ONLY tuyệt đối. Ghi nhận
  việc mới: pipeline ALIGN bản dịch xuất bản về block sau khi chọn cuốn.
- **2026-07-08 (j)** — User chốt: **corpus văn học DUY NHẤT, phải có bản dịch
  người đầy đủ** — không chạy thí nghiệm trên cuốn thiếu gold người rồi phải chạy
  lại cuốn khác. Cây quyết định: TI nếu tìm được edition "Đảo giấu vàng" đầy đủ,
  ngược lại chuyển hẳn sang cuốn Q7 tốt nhất (TI archive; oracle làm lại; tiêu chí
  giàu-quan-hệ thành BẮT BUỘC; memorization test bắt buộc-sớm). Q7 research chuyển
  từ song-song thành CHẶN đường găng L1a/c/d; L1b + Critic T1 spec + cache-hit
  metric chạy trước không cần chờ (corpus-agnostic). Supersedes phân-vai-2-cuốn
  của (h)/(i).

---

## 7. Q7 closed corpus decision - Wuthering Heights / Doi gio hu

**Decision (2026-07-08):** use **Wuthering Heights / Doi gio hu** as the single
literary corpus for the next Builder and evaluation track. Treasure Island is archived
as prior oracle/regression material, not the active literary corpus.

**Evidence recorded in repo:**
- Survey: `reference/literary/Q7_novel_reference_survey.md`.
- EN source manifest: `reference/literary/wuthering_heights/README.md`.
- EN EPUB: `reference/literary/wuthering_heights/en/wuthering_heights_gutenberg_768_epub3_images.epub`.
- VI candidate: `reference/literary/wuthering_heights/vi/doi_gio_hu_vi_full_34ch_candidate.epub`.

**EN source:** Project Gutenberg #768 EPUB3, 34 TOC chapters, SHA256
`3F8B0EF1F30026B979A8CFB2488603ED288E75EDDEDB4407919491C47B649B89`.

**VI reference candidate:** user-supplied legal EPUB, 34 numbered TOC chapters,
SHA256 `6737B7DC333C7795A5BB6987274C78C27425DA8B842C89295AA62C2B5B4B84BE`.
Chapter-1 EN/VI spot check found no abridgement signal. The EPUB does not expose
translator/edition metadata, so ref-based reports must label it as full-structure
candidate with `translator_unverified=true` unless later provenance is added.

**Immediate consequence:** L2A Builder scaffold/pilot uses the EN Gutenberg EPUB only.
The VI EPUB is reserved for later alignment/ref-based evaluation; it is not input to Builder.
