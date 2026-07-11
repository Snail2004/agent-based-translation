# TASK LIT M4d — B4 v2: Story Bible thật (thay pilot ch1)

> **Trạng thái:** DRAFT vòng-1 (Claude, 2026-07-11) — chờ CodeX critique (5.6 Sol xhigh),
> tối đa 2 vòng rồi mới implement. KHÔNG viết code trước khi chốt spec.
> **Bối cảnh:** GATE M4c chốt M2/gpt-5.4 PASS; B4-pilot FAIL 3/5 acceptance
> (xem TASK_LIT_M4c_full_run.md, mục GATE CUỐI). M3 hiện tại là code pilot ch1
> chạy lặp 4 lần — hết tầm đúng dự kiến. Chặn scale 34 chương cho tới khi B4 v2 pass.

## 0. Nguyên tắc khoá (không bàn lại)
- Code KHÔNG làm việc ngôn ngữ: mọi phán đoán identity/phase/xưng hô thuộc LLM;
  code chỉ cơ học tất định (dedup surface, sort, interval bookkeeping).
- Input của B4 = m1 checkpoints (as-of) + digests (đã chốt gpt-5.4) — KHÔNG chạy lại M1/M2.
- Style-free: B4 phát facts + observed vocatives; không quyết xưng hô VN
  (LITERARY_STYLE_PROFILE_V1 xử lý sau, tầng khác).
- Đo trước khi tin: mọi bước LLM mới phải có bộ acceptance case trên ch1-4 trước khi wire.

## 1. Thiết kế đề xuất (Claude, vòng-1)

### 1.1 Identity adjudication = LLM một call/scope, code chỉ ĐỀ XUẤT
- Code (cơ học): group ledger theo surface-core như hiện tại NHƯNG chỉ để tạo
  CANDIDATE cluster + gom evidence (aliases, quotes m1, identity facts từ digest —
  digest ch04 đã chứa "Mrs. Heathcliff = Catherine Linton", "Hareton = Earnshaw cuối").
- LLM (gpt-5.4, 1 call/scope, JSON): mỗi candidate cluster → verdict
  `merge | split | keep_separate | uncertain` + evidence_block per verdict.
  Bao gồm cả fragment (ent_catherine_s, ent_your_servant_zillah, ent_and_mrs_heathcliff)
  và cross-id join (ent_hareton ↔ ent_hareton_earnshaw).
- Code apply: chỉ apply verdict có evidence_block hợp lệ (block_id tồn tại);
  `uncertain` → giữ tách + flag needs_human_review. Mirror pattern Term-Auditor +
  3-gate của §29 (audit-label + no-new-collision).
- Canary bắt buộc: Mrs. Heathcliff KHÔNG được nằm chung entity với Heathcliff;
  Mr. Heathcliff PHẢI chung với Heathcliff; Hareton 2 id PHẢI join; King Lear giữ
  mentioned_historical.

### 1.2 As-of subsetting: bible-as-of-N từ chain checkpoint (máy móc M4b có sẵn)
- Mỗi file `wh_chNN_story_bible.json` = consolidation của ledger as-of N
  (đọc `checkpoints/m1/<=N`) + digests 1..N. KHÔNG đọc m1_report cuối.
- Lý do chọn per-chapter cumulative (thay vì 1 bible cuối + query as-of):
  Translator chương N+1 tiêu thụ đúng file N; cấu trúc file nhỏ, dễ verify;
  vẫn giữ interval để query trong-chương.
- scope/audit.scope = `M3_asof_chNN` (bỏ hardcode ch1); canary set theo scope (§1.4).

### 1.3 Interval + phase: chuẩn hoá pair, đóng interval bằng change-point từ digest
- Pair = tuple KHÔNG thứ tự, sort trước khi so — diệt duplicate
  (lockwood,heathcliff)/(heathcliff,lockwood).
- Nguồn phase = `candidate_transition` + `character_state_changes` của digest
  (LLM đã phán) — code chỉ dựng timeline: phase mới cùng pair → đóng interval cũ
  (`valid_to = trigger_block - 1` theo thứ tự block tuyệt đối).
- Valence fallback hiện tại GIỮ làm tầng cuối nhưng phải dán nhãn
  `phase_source=fallback` như hiện nay và không đè digest-driven phase.

### 1.4 Canary per-scope (acceptance của chính task này)
- as-of ch1: Hareton mentioned_historical (inscription); KHÔNG có Hindley/Zillah/
  Catherine Linton/Nelly trong registry; Heathcliff merge đúng; King Lear chưa tồn tại.
- as-of ch2: Hareton on-stage + join 2 id; daughter-in-law phase tồn tại cho pair
  (heathcliff, mrs_heathcliff/catherine_linton) với evidence b042/b046; King Lear
  mentioned_historical.
- as-of ch3: ghost/diary identity facts có mặt; vẫn không leak entity ch4 (Nelly
  narration ch4). as-of ch4: narrator switch 2 segments; identity facts
  (Mrs. Heathcliff = Catherine Linton; Hareton = Earnshaw cuối) phản ánh trong registry.
- entity_type: không hardcode person — Wuthering Heights/Thrushcross Grange về place
  (nguồn: glossary category của M1, cơ học).

### 1.5 Chi phí
- +1 LLM call identity-adjudication per as-of scope (4 call cho ch1-4, gpt-5.4, ước
  <20k tok tổng — trong quota). 0-API cho phần còn lại. Scale 34 chương: 34 call nhỏ.

## 2. Câu hỏi mở đánh số (CodeX trả lời từng câu, kèm trích code nếu claim)
1. Adjudication 1 call/scope hay 1 call/book-rồi-project-as-of? (per-scope tránh
   future leak trong chính lời phán; per-book rẻ hơn 8x khi scale. Claude nghiêng
   per-scope-cumulative: call as-of N chỉ thấy evidence <=N.)
2. Schema verdict JSON tối thiểu cần field gì để code apply tất định không suy diễn?
3. Đóng interval `valid_to = block trước trigger` có ổn với block numbering per-chapter
   (wh_ch02_b001 sau wh_ch01_b092)? Cần thứ tự block tuyệt đối toàn sách ở đâu?
4. Có case nào code apply verdict LLM tạo collision mới (2 cluster merge vào 1 id
   đã tồn tại)? Cần gate no-new-collision như §29?
5. Fragment garbled (ent_and_mrs_heathcliff) nên `split` về đâu khi surface không
   parse được — quarantine như §30 review_only?

## 3. Điều kiện dừng
- Sau vòng-2: Claude chốt spec cuối, CodeX implement + dry-run 0-API trên ch1-4
  (adjudication call render prompt thật, --confirm trước khi gọi API).
- Gate: Claude tự chấm §1.4 trên artifact thật. PASS → mở đường scale 34 chương.

---
# VÒNG 1 — Claude verify critique của CodeX Sol (2026-07-11): 3 BLOCKER đều CONFIRMED → REV2

Verify từng citation trên artifact thật:
- **B1 (atom = mention-occurrence) CONFIRMED, mạnh hơn trích dẫn:** chính văn bản tự phân biệt —
  ch4 b017 Lockwood: "What! Catherine Linton? ... it was not my ghostly Catherine". Digest ch3
  s_04 dùng `ent_catherine_linton` = hồn ma (Catherine mẹ); ch4 b016 "Catherine Linton was her
  maiden name" = Catherine trẻ. MỘT id, HAI người. Partition trên entity_id không tách nổi.
- **B2 (relation fact có hướng ≠ phase) CONFIRMED:** taxonomy phase hiện chỉ có valence-label;
  "daughter-in-law" là kinship fact có chiều, pair unordered làm mất chiều.
- **B3 (LLM phase segmentation) CONFIRMED:** `_phase_label_from_valence_hint` trong
  `_consolidate_relations` là code dịch valence→label = việc ngôn ngữ. Đính chính nhỏ:
  `candidate_transition` CÓ tồn tại (sub-field optional per-relation, vd ch02 b082
  "she briefly intercedes for his safety") — dùng làm INPUT cho LLM phase call, không thay nó.
- **MAJOR as-of CONFIRMED:** `run_m3` đọc `_load_m1_report` (final); checkpoint ch02
  `artifact_manifest` chỉ chứa 27 artifact của riêng ch02 → as-of phải chain-validate rồi
  UNION manifest ch1..N (tái dùng pattern `_m1_checkpoint_chain_for_m2` có sẵn), cấm glob.
- **Phát hiện thêm khi verify (Claude):** phase (lockwood,heathcliff)@b011 trong bible thật ra
  là (lockwood, ent_mrs_heathcliff) trong digest — bị remap qua merge sai. Digest LUÔN đúng
  ở tầng nó; mọi ô nhiễm nằm ở resolver/merge của M3.
- MAJOR incremental / evidence-gate / M3-checkpoint + MINOR place: CHẤP NHẬN cả 4.
  5 câu trả lời của Sol cho Q1-Q5: nhận cả 5 (per-scope incremental; partition schema;
  block-ordinal map + interval nửa mở; apply-to-copy + gate battery; group_reference/quarantine).

## REV2 — thiết kế chốt để Sol confirm (vòng 2, KHÔNG mở lại hướng đã chốt)

### R1. Atom granularity (điều chỉnh duy nhất của Claude so với đề xuất Sol)
Atom = `(source_entity_id × surface × chapter)` kèm block_ids đầy đủ — KHÔNG per-occurrence
mặc định. Lý do: case Catherine Linton tách ở ranh giới CHƯƠNG; per-occurrence thổi prompt
(ch2 ~90 block); atoms bounded ~30-60/chương. Escape hatch: LLM được phép trả
`mixed_within_atom` cho một atom → atom đó (và CHỈ nó) được nổ ra per-occurrence trong một
micro-call tiếp theo. Sol confirm hay phản chứng bằng một case within-chapter thật.

### R2. Hai stage LLM per-scope (prompt do Claude viết, blockquote version-marker)
- `literary_identity_partition_v1`: input = atoms frontier (mới/có-evidence-mới so với
  checkpoint trước) + partition đã chốt (state, có thể mở lại khi evidence mâu thuẫn) +
  identity facts từ digest (hint, không phải evidence). Output = partition đúng schema Sol Q2:
  `component_id / groups[] {member_atom_ids, canonical_atom_id, referent_kind
  person|place|group_reference|literary_allusion}, status resolved|uncertain|quarantine,
  evidence[] {block_id, quote, source_atom_ids, supports same_identity|different_identity}`.
  Mọi atom xuất hiện đúng 1 lần; LLM không mint id cuối; code mint id tất định từ
  canonical_atom_id.
- `literary_phase_segment_v1`: input = relation_event_summary rows (+ evidence quotes resolve
  từ event_index) + candidate_transition + phases mở as-of trước. Output TÁCH ĐÔI:
  `relation_facts[]` CÓ HƯỚNG {subject_ref, predicate (English mở, style-free, vd
  daughter_in_law_of/landlord_of/servant_of), object_ref, valid_from_block, evidence} và
  `relation_phases[]` pair-level {pair unordered, phase_label enum đóng
  friendly|strained|hostile|tender|ambivalent|unlabeled, transition đóng/mở interval}.
  character_state_changes chỉ là context, không tự mở phase.
- Code sau 2 call: apply vào BẢN SAO → gate battery (exact partition; không atom trùng nhóm;
  không merge khác referent_kind; không self-loop relation; không mất turn/event/vocative;
  quote phải là substring THẬT của block — kiểm bằng code; no-new-collision) → pass hết mới
  publish. Fail gate nào → scope đó đánh dấu quarantine + halt báo Claude, không auto-retry đè.

### R3. As-of + checkpoint M3
Bible-as-of-N: chain-validate m1 checkpoints ch1..N (parent-hash liền, prefix tuyệt đối) →
union artifact_manifest từng checkpoint làm danh sách file ĐƯỢC PHÉP đọc; digest đọc qua m2
checkpoint hash tương ứng. M3 checkpoint per-scope (tái dùng checkpoint.py): M1/M2 input
hashes + prompt hashes + model/config + parent B4 hash + raw response + usage + manifest;
resume = longest valid prefix. Interval nửa mở [valid_from, valid_until); block-ordinal map
toàn sách build từ document (tất định).

### R4. Registry đa loại — tuyên bố rõ, không âm thầm
T2 v2 = registry person-only như cũ; atoms có referent_kind ≠ person đi về:
place → T1 glossary (đối chiếu entry sẵn có), group_reference/quarantine → section riêng
`review_only` (theo §30). Canary WH-place đổi thành: `ent_wuthering_heights` KHÔNG được
nằm trong T2 person registry.

### R5. Acceptance ch1-4 (sửa theo B2 + giữ per-scope canary cũ)
- ch2: relation_fact `ent_mrs_heathcliff --daughter_in_law_of--> ent_heathcliff`
  evidence b042/b046 (KHÔNG phải phase); Mrs. Heathcliff ≠ Heathcliff entity; Hareton join
  2 id; phase (lockwood, mrs_heathcliff) tồn tại riêng, không remap về heathcliff.
- ch3: `ent_catherine_linton`-atom(ch3) thuộc người GHOST/Catherine-mẹ; as-of ch3 chưa có
  Nelly-narration entities ch4.
- ch4: atom Catherine Linton(ch4) thuộc Catherine trẻ (cùng người với ent_mrs_heathcliff —
  đây là chỗ đo BLOCKER-1 fix); narrator switch 2 segments; Hareton = Earnshaw cuối trong facts.
- ch1: registry as-of đúng 10±? entity của riêng ch1 (đếm cụ thể khi implement), Hareton
  mentioned_historical, King Lear literary_allusion (referent_kind mới thay presence-status hack).
- Cost trần pilot ch1-4: 8 call (2/scope) gpt-5.4, ước <45k tok — trong quota ngày.

**Câu hỏi vòng 2 cho Sol (chỉ 3, đóng):**
1. R1 atom granularity chapter-level + escape hatch: confirm hay đưa case within-chapter thật?
2. Schema 2 output R2 đủ để code apply tất định chưa — còn field nào thiếu để gate battery
   chạy không suy diễn?
3. Thứ tự wire: identity partition trước rồi phase (phase dùng partition mới) trong CÙNG scope,
   hay 2 pass toàn sách? (Claude nghiêng: cùng scope, identity trước — phase refs phải trỏ
   final ids của scope đó.)

---
# VÒNG 2 — Claude verify + CHỐT SCHEMA (2026-07-11): nhận toàn bộ findings, R1 bị bác bằng phản chứng thật

Verify trên artifact:
- **BLOCKER atom: CONFIRMED, giàu hơn trích dẫn.** "the master" trong ch4 chỉ 3 người:
  ent_mr_earnshaw (b004/b036/b038/b044), ent_heathcliff (b026), unknown (b016/b035/b039);
  "the young master" b042 = ent_hindley; "the mistress" cũng đổi referent. Đếm mention rows
  khớp chính xác 22/67/86/82 (ch1-4). → **R1 của Claude bị bác đúng quy trình**: atom mặc định
  = (mention_id × block_id), bỏ escape hatch, frontier-incremental là cơ chế bound prompt.
- **MAJOR taxonomy: CONFIRMED.** Design §5.4 đã khoá enum 8 nhãn [allied, friendly, neutral,
  strained, hostile, estranged, dependent, reconciled] — enum rev2 của Claude bị vứt, dùng
  enum design + version hoá qua prompt marker.
- 5 MAJOR + 2 MINOR còn lại: NHẬN CẢ. Stable-ID (reuse_entity_id/supersedes + quy tắc cơ học:
  mở rộng giữ id; split nhánh chứa canonical atom cũ giữ id, nhánh kia mint mới; tie → halt);
  alias_bindings[] trong output identity; predicate_code taxonomy đóng versioned
  (`literary_predicate_taxonomy_v1`, other→review_only); affected-pair replay khi identity đổi
  (mọi pair chứa entity có membership đổi → remap evidence + chạy lại phase từ evidence đầu);
  **2 LOGICAL stages** shard tất định theo component/pair-batch, estimator quyết số call;
  retry taxonomy (transport/parse/schema = 1 retry + lưu raw; semantic-gate fail = KHÔNG
  regenerate → quarantine/halt); quarantine 2 mức (review_only vẫn publish scope;
  blocked_for_runtime khi chạm speaker/relation/address → halt gate).
- Trình tự trong scope (Sol Q3, chốt): atoms as-of N → identity partition → remap evidence →
  affected pairs → phase/fact segmentation → address observed-only → gate trên bản sao →
  atomic publish + M3 checkpoint.

## PROMPT ĐÃ SOẠN XONG (Claude, verify bằng loader thật + grep book-neutral):
- `literary_identity_partition_v1` — blockquote mới trong design §5.2 (extract 2.080 chars/10
  dòng qua load_system_prompt_from_design, KHÔNG leak tên nhân vật/địa danh WH).
- `literary_phase_segment_v2` — thay v1 (chưa từng wire) trong design §5.4: giữ nguyên enum 8
  nhãn + rule change-point, THÊM RELATION_FACTS có hướng với predicate_code đóng 20 mã;
  interval nửa mở valid_until_block. Extract 1.744 chars OK.
- Validator §5.4 bổ sung: fact checks (enum, no self-loop, subject/object ∈ pair, quote là
  substring thật) + retry taxonomy.

## CANARY BỔ SUNG (theo Sol vòng 2):
- **the-master ch4:** atom b026 về group Heathcliff; b004/b036/b038/b044 về group Earnshaw-cha;
  b042 (young master) về Hindley — 1 surface ≥3 group trong 1 chương.
- **stable-ID ch1→ch4:** entity_id của Lockwood/Heathcliff/Joseph GIỐNG HỆT qua 4 as-of scope.

## **SCHEMA LOCKED — GREENLIGHT IMPLEMENT (CodeX Terra max, theo routing):**
2 pha bắt buộc: (1) scaffold + dry-run 0-API — atoms/frontier/sharding estimator/gate battery/
checkpoint M3/render prompt THẬT trên ch1-4, nộp rendered prompts + estimate cho Claude duyệt;
KHÔNG gọi API ở pha này. (2) sau duyệt: API run ch1-4 (--confirm, gpt-5.4, trần theo estimator),
halt nếu technical-retry >10% hoặc bất kỳ semantic gate fail. Acceptance = §R5 rev2 + 2 canary
mới + canary per-scope cũ. D2L-intact: suite xanh + frozen DB hash + prompt D2L byte-identical.

---
# GATE PHA 1 (Claude verify độc lập trên artifact, 2026-07-11): **PASS có điều kiện — DUYỆT estimator 10 call; pha 2 phải có APPLY trước API**

Tự verify (không tin số báo cáo):
- Scope sạch: design doc + builder_pilot.py không bị chạm; file mới đúng khai báo.
- **As-of no-leak: TỰ KIỂM PASS** — prompt identity ch01 zero block-ref ch2-4; "Hareton" xuất
  hiện HỢP LỆ (inscription b012 thuộc ch1). Chain 4 hash m1 + 4 hash m2 ghi trong report.
- **Canary the-master input: PASS** — cả 6 atom "the master" + my/late-master nằm CÙNG shard 01
  (component union-find theo surface+hint giữ họ surface đi cùng nhau); young master b042 shard 02
  có hint ent_hindley. LLM nhìn đủ để tách 3 nhóm.
- **Estimator: số khớp 100%** khi tự tính lại (107.788 prompt / max 13.922 < 14.000 / upper
  169.228 / 10 call). **DUYỆT 10 call** — shard ch03/ch04 identity ×2 là trung thực, ép về 8 sẽ
  cắt evidence. daily-cap: 169k upper < 225k safety NHƯNG chỉ còn ~156k quota hôm nay → hoặc chạy
  ngày quota mới, hoặc chấp nhận upper là trần bảo thủ (thực tế M2 output chỉ ~30% max) — quyết
  khi chạy, halt-resume đã có.
- Prompt render: dùng ĐÚNG blockquote loader (marker dòng đầu), atoms kèm quote_context,
  PRIOR_GROUPS chỉ nhóm linked (bounded), identity_hints = digest chương hiện tại (rationale ghi
  trong code). Phase: pair chuẩn hoá unordered, replay full history cho affected pairs,
  RELATION_FACTS + taxonomy trong system, b046 "my daughter-in-law" nằm ĐÚNG pair
  (heathcliff, mrs_heathcliff) → fact daughter_in_law_of pass được validator subject/object∈pair.
- Validator response: exact-partition, quote-substring trên source thật, evidence bắt buộc cho
  nhóm đa-atom, alias_bindings đủ. Tests: 5/5 mới + 365/365 pipeline suite (tự chạy).
  Frozen DB tự tính lại KHỚP; keyscan sạch; CLI M3V2 từ chối khi thiếu --dry-run.

**ĐIỀU KIỆN PHA 2 (bắt buộc trước khi gọi API):**
1. **[CHÍNH] Implement APPLY + PUBLISH**: hiện CHƯA có run_m3_v2 executor, chưa có code mint id
   tất định + stable-ID (reuse/supersedes/tie→halt) + gate battery trên bản sao + publish story
   bible + checkpoint M3 write. Pha 2 phải implement + **test 0-API bằng response LLM TỔNG HỢP**
   (synthetic: partition đúng, partition thiếu atom, quote sai, reuse id lạ, tie split) TRƯỚC
   khi đốt quota — không để response thật rơi vào code chưa test.
2. Real-run phải BỎ dry_run_note khỏi prompt (hiện đánh dấu rõ — đúng; đừng để lọt vào call thật).
3. Acceptance daughter-in-law neo bằng **b046** (b042 không vào digest event_ids — chấp nhận,
   b042 thành optional).
4. **Watch-item (không blocker):** under-merge xuyên-shard cùng scope — "the old master" b035
   (shard 02) và component "the master/Mr. Earnshaw" (shard 01) không nhìn thấy nhau → có thể ra
   2 group cho cùng người; an toàn nhờ thiên hướng SPLIT (nối lại được) + flag review. Ghi số đo
   trường hợp này ở gate output pha 2; nếu nhiều → cân nhắc pass hợp nhất theo evidence ở scope sau.

---
# GATE PHA 2 (Claude verify độc lập, 2026-07-11): **PASS — apply/publish đạt chuẩn; API cần thêm MỘT driver hook**

Tự verify trên code + tự chạy test:
- `apply_identity_partition_response`: làm trên BẢN SAO, validate trước, split-tie
  (2 group cùng claim 1 reuse id) → halt fail-closed với comment đúng bản chất ("picking a
  branch here would be code performing an identity judgement"); reuse kiểm kind-collision;
  mint tất định (surface-key → suffix hash atom, không bao giờ coi 2 surface bằng nhau);
  non-person/non-resolved → review_only. KHÔNG có suy luận ngôn ngữ trong code. ĐẠT.
- Executor `run_m3_v2_from_responses`: identity apply → remap phase rows sang final ids
  (unresolved → halt blocked_for_runtime) → phase/fact apply (allowed_pairs ràng đúng scope)
  → build story bible → publish gate → ghi atomic → checkpoint SAU publish (kèm raw responses
  + audits + m1/m2 input hash + parent chain) → resume prefix. Guard runtime raise nếu
  dry_run_note còn trong prompt. Fail semantic → report halted, KHÔNG publish scope lỗi.
- Synthetic tests phủ ĐỦ các ca gate yêu cầu: happy-path publish+resume; thiếu atom; quote
  sai; reuse id lạ; split-tie; dry_run_note omitted; book-neutral qua loader runtime; chain
  M2 gãy bị reject; checkpoint contract. Tự chạy: 9/9 focused + 369/369 pipeline suite.
- Client-free THẬT (chỉ import estimate_prompt_tokens — helper đếm, không phải client).
  Frozen DB tự tính KHỚP; keyscan sạch; scope diff đúng 2 file khai báo.

**PHÁT HIỆN GATE — thiếu MỘT mảnh wiring cho bước API (không phải lỗi, là việc kế):**
Prompt runtime scope N+1 phụ thuộc state ĐÃ APPLY của scope N (frontier prior_groups từ final
ids), nhưng executor nhận responses_by_scope trọn gói và halt `scope_responses_missing` không
kèm rendered prompts → driver API bên ngoài KHÔNG có cách lấy đúng prompt để gọi model, và tự
render lại ngoài executor sẽ rủi ro lệch prompt-gọi vs prompt-apply.
**Yêu cầu (chọn phương án hook):** thêm tham số optional `request_llm(messages, meta) -> response`
vào executor — khi scope thiếu response thì gọi hook NGAY TRONG vòng lặp (prompt byte-identical
với bản apply), persist raw response + usage trước khi apply; không có hook → giữ hành vi hiện
tại. Hook nối vào LLM client cache sẵn có (sqlite cache, usage log, confirm-usd, halt khi
technical-retry >10%). Equivalence check bắt buộc: runtime prompt ch01 (state rỗng) phải ==
dry-run shard ch01 TRỪ dry_run_note.
**Sau hook + test 0-API cho hook (mock client): ĐƯỢC PHÉP chạy API 10 call gpt-5.4** (config
m4full_m2_gpt54 nhánh riêng out_dir literary_m4d_b4v2, --confirm, quota ngày mới). Gate output
theo acceptance §R5 + 2 canary vòng-2 + watch-item under-merge xuyên-shard + tiêu chí B0
embedded-cast (ch03 dream-cast phải ra literary_allusion/mentioned, không thành on-stage person).

---
# GATE HOOK (Claude verify độc lập, 2026-07-11): **PASS — DUYỆT CHẠY API 10 CALL gpt-5.4**

Tự verify:
- Hook `request_llm` gọi NGAY TRONG vòng apply → prompt-gọi == prompt-apply (byte-identical,
  đúng yêu cầu gate pha 2). Raw response + usage persist ATOMIC trước apply ở MỌI nhánh
  (thành công / exception / contract sai / parse fail). Attempt-2 bypass_cache; lỗi kỹ thuật
  (transport/parse) tách hẳn semantic gate — semantic KHÔNG regenerate, đúng taxonomy.
- Halt technical-retry >10%: M3_V2_MAX_TECHNICAL_RETRY_RATE=0.10, kiểm trên accounting combined,
  raise technical_retry_rate_exceeded. --confirm-usd bắt buộc, CLI exit 1 TRƯỚC khi khởi tạo
  client. Adapter make_m3_v2_request_llm dùng LLMClient cache sẵn có (JSON contract test riêng).
- Equivalence test runtime-ch01 == dry-run trừ dry_run_note: CÓ, pass.
- Tự chạy: 13/13 focused; full pipeline suite 373/373 (một fail chroma ở lần chạy đầu là FLAKE
  isolation — rerun đơn lẻ pass + rerun suite pass, không thuộc scope thay đổi).
- Frozen DB tự tính KHỚP; keyscan 3 file sạch; scope diff đúng khai báo.

**DUYỆT API:** chạy 10 call gpt-5.4 trên ch1-4 — config `llm_prepass_m4full_m2_gpt54.yaml`
(nhánh cache/out riêng: out_dir data/reports/literary_m4d_b4v2, cache literary_builder_cache_
gpt54_key2), `--milestone M3V2 --confirm-usd`, ngày quota mới (upper 169k < 225k safety).
OPENAI-KEY-2, xoá key khỏi env sau run. Nộp gate: m3_v2_report + 4 story bible + chain
checkpoint + raw responses + usage thật per-call. Claude chấm acceptance: the-master 3 nhóm /
stable-ID ch1→ch4 / daughter_in_law_of neo b046 / King Lear literary_allusion / narration ch4
2 segment / đếm entity as-of ch1 (no-leak) / watch-item under-merge xuyên-shard (ĐO số ca) /
tiêu chí B0 embedded-cast (dream-cast ch03 không thành on-stage person).

---
# GATE MỞ KHOÁ SCALE (user chốt 2026-07-11): review kép SAU run API, TRƯỚC 34 chương
Sau khi 10 call chạy xong và Claude gate output (7 mục acceptance):
1. **Sol max — architecture review as-built trên ARTIFACT THẬT**: toàn bộ story_bible_v2.py
   + 4 story bible thật + raw responses + checkpoint chain. Câu hỏi định hướng: (a) validator
   có quá chặt/quá lỏng so với hành vi THẬT của gpt-5.4; (b) under-merge xuyên-shard đo được
   bao nhiêu ca, có cần pass hợp nhất; (c) frontier-incremental có giữ được bound khi ngoại
   suy 34 chương (số liệu shard thật); (d) khoảng trống nào giữa Story Bible schema và nhu cầu
   Translator/address-policy tầng kế.
2. **Claude — gate độc lập như thường + verify từng citation của Sol.**
3. Hai bên đối chiếu finding; mâu thuẫn → vòng 2; đồng thuận → USER quyết mở khoá 34 chương.
Lý do KHÔNG review trước run (đã quyết): 5 lớp soi đã qua; run 10-call là probe fail-closed
rẻ nhất; hành vi model thật là ẩn số duy nhất còn lại mà không review tĩnh nào trả lời được.

---
# GATE CALL THẬT #1 (Claude, 2026-07-11): fail-closed ĐÚNG THIẾT KẾ — lỗi là FORMAT-SLIP
# trường phụ, fix = normalize hẹp trong code, PROMPT GIỮ NGUYÊN KHOÁ

Soi raw response thật (m3_v2/wh_ch01/...shard_01_attempt_01.json):
- **Phần phán đoán của model ĐÚNG**: 22/22 member_atom_ids full-form, exact partition 6 nhóm
  (11 Heathcliff-cluster / 3 / 5 Joseph / 1 Hareton-inscription / 1 / 1 uncertain) — nội dung
  hợp lý. Trượt CHỈ ở evidence.source_atom_ids: 17/19 dùng dạng ngắn `atom_m_..._01` thiếu
  suffix `__wh_ch01_bXXX` (2 cái còn lại full-form — slip không hệ thống).
- **Gốc rễ**: atom_id hiện THỪA thông tin (mention_id đã chứa block: `atom_m_wh_ch01_b011_01__
  wh_ch01_b011`) — model nén phần thừa ở section verbose. Lỗi format trường PHỤ (provenance),
  không phải lỗi ngôn ngữ/danh tính.
- Kỷ luật giữ đúng: semantic gate KHÔNG regenerate (1 call, 0 retry), raw persist đầy đủ,
  không publish gì. Chi phí 7.678 tok NẰM TRONG CACHE — không mất.

**QUYẾT ĐỊNH (theo pattern validator-fix-field-keep-item-not-drop, variant 5):**
- **Fix CODE, hẹp**: trong validate/apply identity, evidence.source_atom_ids dạng ngắn được
  chấp nhận KHI VÀ CHỈ KHI mention_id đó map về ĐÚNG MỘT atom trong tập atom của shard
  (đã đo: 1/257 mention ch1-4 có >1 block → nhập nhằng THẬT tồn tại, ca đó vẫn hard-fail).
  Đếm + surface `evidence_atom_id_normalized` trong audit/report — số cao ở 9 call còn lại là
  tín hiệu theo dõi, không phải để mừng.
- **PROMPT GIỮ NGUYÊN** (đã khoá): normalize tất định là lớp robust (tiền lệ B2
  attribution_method — prompt cấm rồi model vẫn trượt); và giữ nguyên prompt bytes để re-run
  ch1 shard 1 ăn LLM-CACHE → response cũ được TÁI DÙNG $0, sau normalize sẽ PASS.
- Test: fixture = CHÍNH raw response thật bị reject; + ca ngắn-nhập-nhằng phải reject;
  + ca ngắn-không-tồn-tại phải reject. member_atom_ids/alias KHÔNG normalize (chưa thấy slip
  — mở rộng phải có bằng chứng mới, không normalize phòng hờ).
- CodeX: **Luna max** (fix nhỏ, spec chặt). Sau fix + test → chạy lại M3V2 (resume; kỳ vọng
  call ch1-identity from_cache=true, $0, rồi đi tiếp 9 call thật).

---
# CHỐT AMENDMENT #2 (Claude, 2026-07-11): validator ĐỌC SAI ngữ nghĩa evidence — NỚI invariant,
# KHÔNG lọc atom, GIỮ provenance nguyên văn

Soi 4 evidence bị bác trên raw thật: b019 "Joseph mumbled... so HIS MASTER dived down" và
b020 "MR. HEATHCLIFF and HIS MAN climbed" — mỗi câu giải quyết danh tính cho HAI nhân vật cùng
lúc (his master→Heathcliff NEO QUA Joseph; his man→Joseph neo qua Heathcliff). Model trích cùng
câu cho cả hai nhóm, kê đủ atoms câu đó resolve = cách annotator người làm. Check
`same_identity source_atom outside_group` đọc nghĩa đen "mọi atom = một người" là ĐỌC SAI ngữ
nghĩa: source_atom_ids là "các atom mà quote này resolve", không phải "các atom cùng một người".
(Variant-4 validator-wrong, đo trên call thật; check này cũng KHÔNG thuộc schema đã khoá —
là strictness CodeX thêm khi implement, nới nó không mở lại lock.)

**Quyết định — phương án 3 (không phải 2 phương án CodeX nêu):**
- KHÔNG lọc atom khỏi evidence (mất provenance — đúng lo ngại của CodeX).
- KHÔNG cho qua vô điều kiện: giữ các check (a) atom tồn tại sau normalize, (b) không duplicate,
  (c) must_touch_group (≥1 source atom thuộc group — check sẵn có).
- NỚI đúng một điều: same_identity ĐƯỢC PHÉP chứa atom thuộc NHÓM KHÁC (câu resolve chéo),
  đếm + surface `evidence_cross_group_source_atoms` trong audit/report (kỳ vọng =4 trên ch1).
- Test: fixture = raw response thật → sau normalize(17) + relax phải PASS TRỌN;
  same_identity trỏ atom KHÔNG TỒN TẠI vẫn reject; prompt GIỮ KHOÁ → resume ăn cache $0.

---

## AMENDMENT #3 (2026-07-11, Claude gate) — phase prompt micro-change: OUTPUT JSON line (API compatibility)

**Trigger (real artifacts):** resume after amendment #2 halted at `halted_technical_gate` / `request_llm_parse_or_transport_failed`: OpenAI 400 `'messages' must contain the word 'json' ... to use 'response_format' of type 'json_object'` on `literary_phase_segment_v2` wh_ch01 shard 1, attempts 1+2 (raw retained, usage 0/0 — no tokens billed, no publish/checkpoint). Identity ch1 replayed from cache as designed ($0). Verified: the phase blockquote contained NO literal "json" (identity prompt has it via its Output-JSON-only line); the rendered user payload keys don't contain the substring either.

**Decision (APPROVED, prompt owner = Claude):** append exactly ONE line to the `literary_phase_segment_v2` blockquote:

`OUTPUT: JSON only — one JSON object with exactly two keys, "relation_phases" and "relation_facts", holding the lists described above; every phase includes its pair. No text outside the JSON object.`

- Fixes the API requirement (literal "JSON" in messages) — a transport/compatibility constraint, not a semantic change.
- Also closes a latent gap: the prompt never declared the output envelope; only the user payload's `response_envelope` did. The line states exactly the keys the parser/validator already requires (`relation_phases`, `relation_facts`, phase includes `pair`) — alignment with existing code, no new schema.
- Verified via REAL loader (`load_system_prompt_for_chapter`, PHASE_SEGMENT_VERSION): rendered prompt now contains "JSON"; identity prompt untouched → identity cache keys preserved.
- Cache impact: phase prompts get a new cache key — acceptable, no successful phase response exists to lose. Identity ch1 stays $0 on resume.
- Amendment #2 relax code independently gated: diff removes ONLY the `same_identity outside_group` check, keeps exists/no-dup/must_touch_group, surfaces `evidence_atom_id_normalized` + `evidence_cross_group_source_atoms`; focused tests re-run by Claude 15/15.

**Next:** CodeX re-runs `--resume` (no code change needed — prompt loads from design doc at runtime). Expect: identity ch1 from_cache, phase ch1 real call, then remaining scopes.

---

## AMENDMENT #4 (2026-07-11, Claude gate) — TWO wiring defects found after resume; ch1 checkpoint INVALID; fixes are mechanical, NO new LLM stage, NO prompt change

### Defect A (SILENT — worse): runtime phase payload was EMPTY; ch1 published a bible with 0 relations that "passed"

Evidence (real artifacts): phase ch1 call = 525 prompt tokens vs dry-run estimate 5,689 for the same scope; model returned `{"relation_phases":[],"relation_facts":[]}` (17 tokens) — correct behavior for the input it was given. Real m4_full digest ch1 has 3 pairs / 18 event_ids, ALL 18 join the event index; the evidence never reached the model.

Root cause: type mismatch between scaffold and runtime batching. `scope["phase_rows"]` is already the output of `_phase_pair_batches` (batches `{provisional_pair, history}`), but `_runtime_phase_pair_batches` expects FLAT rows keyed by `source_chapter_id` → `affected` = empty set → zero pair batches → `pair_evidence: []` sent. Dry-run sized the CORRECT payload; runtime built a DIFFERENT one. Green equivalence tests missed it because both compared sides were built from the same wrong path.

Fix (CodeX, no judgment involved):
1. `_runtime_phase_shards` consumes the MAPPED batches directly (affected-pair filtering already happened at scope build; delete the re-derivation from `source_chapter_id`).
2. Fail-closed guard: runtime (`scaffold_only=False`) phase request with non-empty mapped pairs but zero history events → raise wiring error (technical halt), never send.
3. Surface counters in phase audit/report: `phase_pairs_sent`, `phase_history_rows_sent`, `phase_events_sent` (ch1 expected: 3 / 3 / 18).
4. **Invalidate ch1**: delete `checkpoints/m3_v2/wh_ch01.json` + `story_bible_v2/wh_ch01_story_bible.json` (published from an empty-evidence run — invalid regardless of what a correct run would output). Resume re-runs ch1: identity replays from local cache ($0, prompt untouched); phase ch1 is a new real call (~5.7k in est).
5. Tests: (a) runtime rendered phase prompt for a synthetic scope MUST contain a known evidence quote from the history events; (b) scaffold-vs-runtime payload row/event count equivalence; (c) the empty-payload guard fires on a constructed empty batch.

### Defect B (loud, correct halt at ch2): provisional digest pair id `ent_hareton` has no binding to final ids

Evidence: ch2 digest `relation_event_summary` names the same person under THREE provisional ids (`ent_the_young_man`, `ent_hareton_earnshaw`, `ent_hareton`). The first two resolve via atom `hint_entity_id`; `ent_hareton` never appears as a hint because every "Hareton" B1 mention is `named` with empty `candidate_entity_ids`. `_remap_phase_rows_to_final_ids` fail-closed correctly.

Decision: **REJECT the proposed new LLM binding hand-shake** (identity stage returning provisional→final mappings). Reasons: (1) it would change the LOCKED identity prompt → invalidates identity caches for ch1+ch2 (~19k+ tokens re-billed) and adds schema churn mid-run; (2) it re-asks the model a judgment it already made at atom level; the mapping is derivable by COMPOSING two recorded LLM judgments — that is bookkeeping, not language work (the inverse of the Mrs.-Heathcliff lesson: code may follow LLM identity decisions, it may not make them).

Mechanical rule (scope-level, after identity apply, fail-closed):
- For each unresolved provisional id P: over all summary rows whose pair contains P, over their JOINED events, find P's side per event mechanically — (i) `side.candidate_entity_ids == [P]`, or (ii) by elimination when the other side resolves to the pair-mate's final id. Resolve that side ONLY via existing `_resolve_final_entity_ref` paths (hint binding / unique candidate / exact same-block atom surface).
- Collect distinct final ids across witnesses. Exactly one → bind P→final for THIS scope; record `provisional_bindings` in audit with witness event/block ids. Zero or ≥2 → stay unresolved → halt exactly as today (design an escalation only if a real corpus case demands it — measure first).
- Expected on real ch2: `ent_hareton → ent_hareton_earnshaw` via witness `e_wh_ch02_b058_01` (target surface "Hareton"@wh_ch02_b058 = atom `m_wh_ch02_b058_01`, which the identity partition placed in `ent_hareton_earnshaw`). Both halted pairs then map.
- Tests: real ch2 fixture (digest summary + joined events + real identity raw response) produces exactly this binding; synthetic two-conflicting-witnesses → halt; synthetic zero-witness → halt.

### Run plan after both fixes
No prompt changes anywhere → identity ch1 AND ch2 replay from local cache at $0; only phase calls (and ch3 identity) are new spend. Resume order: ch1 (invalidated, re-publishes with real evidence) → ch2 → ch3. Report must show the new counters so the phase payloads are auditable per run.

Method note for the thesis log: defect A is the strongest instance yet of audit-real-rendered-prompts — a green, published, validator-passing artifact was invalid, and the ONLY visible symptom was a token count (525 vs 5,689 estimate) on a real call.

---

## AMENDMENT #4b (2026-07-11, Sol mandatory amendment — Claude VERIFIED & APPROVED) — Fix A must merge by FINAL pair; per-pair guards; event-join integrity

Sol's review of amendment #4 raised one new BLOCKER + one MAJOR. Claude verified every claim on real artifacts before approving:

**BLOCKER (confirmed):** "use mapped batches directly" is insufficient. Real ch2 digest has TWO provisional pairs that collapse to ONE final pair:
- `ent_the_young_man ↔ ent_mr_lockwood` (e_wh_ch02_b013_01, b029_01, b048_01)
- `ent_hareton_earnshaw ↔ ent_mr_lockwood` (e_wh_ch02_b053_01, b080_01)
Verified: atom "the young man"@b079 hints `ent_the_young_man`; identity group `grp_hareton_earnshaw` (18 members, incl. b013/b050/b053 atoms) holds both families → both rows map to (ent_hareton_earnshaw, ent_mr_lockwood). Sending two batches for one pair gives the model two disjoint timelines of the same relationship → duplicate/conflicting phases (the validator's duplicate-open-interval check would halt AFTER paying for the call — fix the input, don't rely on the output gate).

**Required pipeline:** mapped provisional batches → GROUP BY final pair → concatenate histories (deterministic order: source_chapter_id, then original row order; events keep their block ids for text-order reading) → shard → send. `allowed_pairs` = final pairs after merge.

**MAJOR (confirmed):** guards must be PER final pair, not per request: every final pair must carry ≥1 history row and ≥1 joined event, else wiring halt. Additionally `_phase_rows_as_of` line ~575 silently drops event_ids missing from the event index (`if eid in event_index`) — silent evidence loss. Missing join → hard-fail with the offending ids listed (no silent drop, no partial prompt). Escalation policy only if real corpus shows benign frequency — measure first.

**Pre-measurement (Claude, offline):** all cited summary event_ids join the index for the current inputs — ch1 18/18, ch2 38/38, ch3 23/23, missing=0. The hard-fail guard will not fire on this run; it protects the 34-chapter scale.

**Counters (report + audit):** `provisional_pair_batches`, `final_pairs_sent`, `collapsed_pair_batches`, `history_rows_sent`, `events_sent`. Expected ch2: provisional≥9-row-derived batches with `collapsed_pair_batches ≥ 1` (the_young_man+hareton_earnshaw collapse); ch1: 3/3/0/3/18.

**Tests:** compare event_id SETS before remap vs after merge (must be equal — nothing dropped, nothing invented), not just counts or a sample quote; collapsed-pair fixture from the REAL ch2 rows must yield ONE batch for (ent_hareton_earnshaw, ent_mr_lockwood) with all 5 events; per-pair empty guard fires on a constructed empty batch; missing-join fixture hard-fails listing the id.

**Housekeeping (approved):** delete ch1 checkpoint + story bible only; KEEP the empty-payload raw responses as audit evidence. Identity ch1+ch2 expected cache-hit (no identity input changes). Bug B stays as amendment #4 (witness composition, zero-witness/multi-final still halt).

---

## AMENDMENT #5 (2026-07-11, Claude gate) — phase_response_rejected ch1: 3 punctuation slips + 1 validator-stricter-than-locked-spec; model judgment CORRECT on all 4; fixes validator-side only, cached response replays at $0

**Verified on real artifacts.** Phase ch1 real call (4,316 in / 577 out) produced 4 phases + 4 relation facts, all interpretively sound (joseph dependent/servant_of heathcliff; joseph–lockwood strained; heathcliff–lockwood strained b003–b025 closed → friendly open from b026; landlord_of/tenant_of at b005). The 4 validator errors:

1. **3 × apostrophe (mechanical):** model wrote ASCII `'` in "Mr. Lockwood's horse" (1 phase trigger + 2 facts, all b008); source b008 has curly `’`. Content verbatim otherwise.
2. **1 × trigger_evidence location:** quote "you are flurried, Mr. Lockwood" is real and verbatim — at b027, INSIDE the phase's range (b026 → open). Source b026 = "Heathcliff's countenance relaxed into a grin" → the model's claim that the friendly phase STARTS at b026 is defensible interpretation. The LOCKED design contract (§5.4 code-validate line) requires "mọi trigger có evidence trong range" — evidence within the RANGE. The implemented check (line ~2739) demands substring of trigger_block specifically — stricter than spec. Validator wrong, model right (variant-4; cf. amendment #2).

**Decisions (all validator-side; prompt LOCKED untouched → cached phase response replays $0):**

1. **Punctuation-fold quote locating (uniform helper, all quote gates — identity evidence, phase trigger_evidence, fact evidence_quote):** when exact substring fails, fold Unicode punctuation (’‘→', “”→", —–→-) on both sides to LOCATE; on a UNIQUE folded match, restore the SOURCE-verbatim substring into the stored artifact (source text is the authority) and count per-stage `evidence_quote_punct_normalized`. No match / ambiguous → reject as today. Precedent: amendment #1 unique-mapping normalize; D2L scorer casefold+punctuation hardening.
2. **Range check per locked spec:** trigger_evidence must locate verbatim (after fold) in ANY block of the phase's range [valid_from_block .. valid_until_block, or scope end when open]. Keep `trigger_block must_equal_start`. Record the located block as `trigger_evidence_block` on the stored row (surfaced provenance). Do NOT auto-move trigger_block (phase start = model's interpretive claim, untouchable by code).
3. **Raw-file overwrite (audit finding — CONFIRMED, fix REQUIRED before next resume):** resume overwrote `literary_phase_segment..._attempt_01.json`, destroying the empty-payload evidence file (still recoverable in SQLite llm_call_cache; amendment #4 + report preserve the record). Raw filenames must be append-only across resumes: scan existing files and continue the attempt sequence (or embed a resume/run id); open exclusively — NEVER overwrite an existing raw file.

**Expected on resume:** ch1 identity + phase both from cache ($0); after normalize(3) + range-locate(1) the response fully passes → ch1 publishes 4 phases + 4 facts; counters `evidence_quote_punct_normalized=3`, trigger located at b027. Then ch2 (identity cache-hit, phase real call with merged final-pair batches, `collapsed_pair_batches ≥ 1`) → ch3. Test: real ch1 phase raw as fixture must fully pass; ASCII-vs-curly fixture keeps rejecting on ambiguous/multi-match; range-located block outside range still rejects.

---

## AMENDMENT #6 (2026-07-11, Claude gate) — ch3 identity truncated at output ceiling; raise cap via strict-hash rebuild ($0 for ch1/ch2); poisoned-cache accounting

**Verified on real artifacts:** ch3 identity shard 1, BOTH attempts completion_tokens = 6,144 exactly (= configured `max_output_tokens`), JSON `Unterminated string` ~char 19.8-20.2k. Not a model error — the frontier (86 atoms, dream/diary chapter with embedded cast) legitimately needs more output than ch2 (which already used 5,867/6,144 = 95.5%). Bonus measurement: attempt 2 hit the server prefix cache for 14,080/14,535 prompt tokens (97%) — retries are near-free on the prompt side.

**Decisions:**

1. **Raise M3 v2 `max_output_tokens` 6,144 → 12,288** (single config value; ceiling ≠ spend). Quota check: worst-case remaining calls ≈ 7 × 12,288 out + ~71k prompt ≈ 157k < 198,790 headroom.
2. **Do NOT weaken `_m3_v2_config_hash`** (the cap stays hashed — it CAN alter a response by truncating it). Ch1/ch2 checkpoints become stale BY DESIGN. This is affordable because the LLM replay-cache key (llm_client.py:87-96) does NOT include `max_output_tokens` → on resume the prefix rebuild replays identity+phase ch1/ch2 from cache at **$0**, re-validates all gates deterministically, republishes with the new config_hash. EXPECTED resume_mismatch: ch1 `fields=["config_hash"]` ONLY — any other mismatched field → halt and investigate.
3. **Poisoned-cache accounting:** the truncated ch3 response sits in the replay cache under a cap-independent key. On resume, attempt 1 replays it, parse-fails at $0, and the existing bypass_cache retry then makes the real call under the new cap — correct flow, BUT the current 10% technical-retry-rate guard would count that $0 replay failure as a retry and could halt spuriously (~1/8 attempts = 12.5%). Fix: a cache-replayed response that fails parse/validation is counted `poisoned_cache_replays` (surfaced per mode/shard) and EXCLUDED from `technical_retry_rate`, which exists to guard FRESH spend. Truncated raws stay on disk (append-only) as audit; no manual cache deletion.
4. **Tests:** (a) config-bump prefix rebuild — synthetic checkpoints with old config_hash + stocked replay cache → rebuild publishes identical state, zero fresh calls; (b) truncated cache replay classified poisoned → bypass retry proceeds; (c) retry-rate fixture where the old accounting would halt at 12.5% now passes with poisoned excluded and still halts on real fresh-call retries over 10%.

**Expected resume:** ch1/ch2 republish from cache ($0, 4 replay hits), ch3 identity s1 fresh (≤12,288 out; prompt ~97% server-cached), s2 + phase ch3 fresh → full ch1–3 published → Claude acceptance gate + Sol dual review (scale-unlock).

---

## AMENDMENT #7 (2026-07-11, Claude gate) — ch3 identity reject: mechanical resolution ladder for unknown reuse ids + wrong-suffix atom repair; the model's semantics are CORRECT (diary-frame Heathcliff, two Catherines kept apart)

**Verified on the fresh parsed response** (`..._attempt_02_resume_01.json`, 6,430 out < new cap; shard 2 fully valid, all 5 reuses correct). Shard 1's rejects decompose into three mechanical classes — none is a judgment error:

**What the model actually did (verified):** grp1 = diary-frame Heathcliff (4 atoms, all in Catherine's diary blocks b006–b018) with reuse `ent_heathcliff`; grp2 = present-day Heathcliff (13 atoms) with reuse `ent_mr_heathcliff`; grp3 = Catherine Earnshaw the mother (16 diary/ghost atoms, canonical surface "Catherine Earnshaw") minted-by-name; grp5 = Hindley ("Hindley"); **the two Catherines stay separated** (mother = new entity; daughter = ent_mrs_heathcliff, reused correctly in shard 2). The digest's provisional vocabulary is exactly where the invented id strings come from.

### Resolution ladder (code, fail-closed, in order):

1. **Wrong-suffix atom-id repair** (extends amendment #1): a full-form atom id not in the atom set, whose mention-prefix maps to exactly ONE real atom → replace with the real id; now applies to `member_atom_ids` AND evidence `source_atom_ids`. Safety net that #1 lacked for members: the exact-partition + no-duplicate checks run AFTER repair — a wrong repair cannot survive them. Verified on real data: the 4 true atoms appear nowhere else (65/65 members unique). Counter `atom_id_suffix_repaired` (expect 4 members + 2 evidence refs).
2. **Unknown `reuse_entity_id` — hint composition:** claimed id is NOT an existing entity but has a UNIQUE binding in `hint_to_entities` → rewrite to the bound final id. This composes two recorded LLM judgments (ch2: atoms hinting ent_heathcliff = ent_mr_heathcliff; ch3: this group continues ent_heathcliff) — code holds no identity opinion. Verified: ch2 map has `ent_heathcliff → [ent_mr_heathcliff]` unique. Counter `reuse_hint_normalized` (expect 1).
3. **Unknown reuse — mint-equality:** else, if the claimed id EQUALS the id `_mint_entity_id` would deterministically produce for this group → treat as null+mint (the claim is contentless: the id exists nowhere, and the resulting state is identical to a fresh mint; the model merely pre-computed our mint). Verified: "Catherine Earnshaw" → ent_catherine_earnshaw, "Hindley" → ent_hindley. Mint collision with an existing entity still halts (existing check). Counter `reuse_mint_equivalent` (expect 2).
4. **Duplicate reuse after normalization — union by the model's own claims:** two groups resolving to the SAME existing entity, with NO `different_identity` evidence linking them (verified: none) and same `referent_kind` (person=person ✓) → mechanical union (members/aliases/evidence concatenated, deduped). This does NOT erase the diary/present distinction — frames live in `narration_frame_segments`, not in entity splits; splitting one person into two entities would contradict ch2's recorded binding. Any conflict (different_identity link, kind mismatch) → `stable_id_split_tie` halt exactly as today. Counter `reuse_duplicate_unions` (expect 1).
5. Anything left unresolved → reject as today.

**Prompt LOCKED untouched → the parsed response is cached; resume replays ch3 identity at $0.** Remaining fresh spend: ch3 phase only (~11.6k est prompt). Quota headroom 167,373.

**Tests:** real ch3 s1 raw as fixture must fully pass through the ladder with exactly the predicted counters; ambiguous mention-prefix (2 candidate atoms) → reject; claimed id ∉ hints and ≠ mint → reject; duplicate reuse WITH a different_identity link → split-tie halt; partition violated by a repair → reject.

**Acceptance watch-items (recorded now, reviewed at the ch1–3 gate):** (a) atom `m_wh_ch03_b046_01` surface "your guest, sir" placed in the Heathcliff group — referent plausibly Lockwood (one-atom precision case, review not halt); (b) ch2 hint oddity `ent_zillah → [ent_mrs_heathcliff]` (an atom hinting zillah was judged Mrs. Heathcliff; harmless to the ladder because ent_zillah IS an existing entity so rule 2 never fires for it — but review the underlying ch2 assignment); (c) shard-2 `grp_catherine_ghost` (uncertain, 4 atoms) vs shard-1 ent_catherine_earnshaw — the predicted cross-shard under-merge case, goes to the measured under-merge list.

---

## CLAUDE GATE — ch1–3 published bibles (2026-07-11) — VERDICT: PASS with ONE mechanical render defect (Amendment #8) + documented review list

### Acceptance scorecard (all verified on the published artifacts, not reports)
- **Stable IDs ch1→ch3: PASS** — ent_mr_lockwood / ent_mr_heathcliff / ent_joseph / ent_hareton_earnshaw / ent_mrs_heathcliff identical strings across all three bibles.
- **As-of no-leak: PASS** — T2 entities 5 → 7 → 13 monotone; ch1 bible knows nothing of ch2+ cast (the M4c leak class is dead).
- **daughter_in_law_of: PASS** — `ent_mrs_heathcliff daughter_in_law_of ent_mr_heathcliff` + inverse father_in_law_of, both anchored `wh_ch02_b046`, published. This is the exact case the M4c honorific-merge destroyed.
- **King Lear: PASS** — `ent_king_lear` referent_kind=literary_allusion, review_only, NOT a T2 person.
- **Two Catherines: PASS** — mother = ent_catherine_earnshaw (T2, 16 diary/ghost atoms, aliases incl. all three surnames); ghost cluster = ent_that_minx (uncertain → review_only, kept OUT of runtime); daughter = ent_mrs_heathcliff distinct.
- **"The master" early test (ch3): PASS with 1 mis-atom** — model built a DEAD-FATHER entity (ent_t_maister: "T' maister nobbut just buried" b012 + "th' owd man…but he's goan" b014 + "our father" b018) and kept it separate from "Maister Hindley" (b014_01 → ent_hindley). One atom mis-joined: "the surly old man" b015 = Joseph ("he may believe his prophecy" — the prophecy is Joseph's). Review, not halt.
- **Embedded-cast (B0 criterion): PASS at entity level, GAP at frame level** — diary cast became T2 entities with correct diary-anchored evidence (they ARE story persons; two-Catherine safe). BUT M2/B3 emitted exactly ONE narration_frame_segment (frame_present, whole chapter) — the diary b005–b018 is NOT marked as an embedded frame. Consequence: diary-time phases (catherine↔heathcliff friendly from b018) sit on the same block-timeline as present-time phases with no frame separation → story-time vs block-time tension lands on address-policy later. NOT a B4 defect (bible renders M2 faithfully). → mandatory agenda item for the Sol dual review (guiding question 4).
- **Counters: all as predicted** — suffix_repaired 4 members + 2 evidence (+4 alias-binding repairs, correctly surfaced separately), hint_normalized=1, mint_equivalent=2, duplicate_unions=2 (second = cross-shard same-entity union of ent_mr_heathcliff — mechanically expected under sharding), provisional_bindings=1.

### AMENDMENT #8 (mechanical, 0 API): valid_to_block render defect
State rows carry `valid_until_block` CORRECTLY (e.g. joseph–lockwood strained closed at wh_ch03_b021; heathcliff–lockwood strained closed at wh_ch03_b047) but published `entity_relations` emit `valid_to_block: null` — the render never maps `valid_until_block` → `valid_to_block` (design §5.4 output contract is `valid_to_block|null`). Closed phases in the bible currently look open-ended. Fix the mapping (one canonical field), re-render ch1–3 bibles FROM EXISTING CHECKPOINTS (0 API, no resume), test: every status=closed row in a published bible has a non-null end block equal to the state's valid_until_block.

### Review list (documented, NOT blocking; Auditor §5.6 / human-review fodder — note the pattern: epithet-surface atoms are where precision drops)
1. ent_madam (ch1, T2!) — single atom b019, Lockwood's sarcastic address to the DOG; model marked resolved person. Should have been uncertain. Phantom T2 entity.
2. "your guest, sir" b046 atom inside ent_mr_heathcliff group (referent plausibly Lockwood).
3. "the surly old man" b015 atom inside ent_t_maister (= Joseph).
4. Two Jabez entities (ent_jabez_branderham 9 atoms + ent_the_reverend_jabez_branderham 1 atom) — cross-shard under-merge, safe direction.
5. ent_cathy provisional bound to ent_that_minx (ghost cluster) not ent_catherine_earnshaw — under-merge; the pair correctly stays out of runtime.
6. Re-segmentation as-of ch3 dropped ch1's brief heathcliff–lockwood "friendly" beat (model's full-history judgment, consistent with its own "momentary mood ≠ phase" instruction; per-scope bibles preserve each as-of view).

### Next steps (locked order)
1. CodeX: Amendment #8 render fix + re-render ch1–3 (0 API); then COMMIT the full amendment #5–#8 batch (code + tests + artifacts + raws).
2. Extend scope to wh_ch04, resume (ch1–3 restore from checkpoints; only ch4 calls fresh — est ~25–35k tokens vs 157,825 headroom). Ch4 carries the OFFICIAL locked "the master = 3 distinct groups" criterion.
3. Claude full acceptance gate on ch1–4 → Sol (max) dual review with the frame-layer agenda + review list → user decides 34-chapter unlock.

---

## AMENDMENT #9 (2026-07-11, Claude gate) — ch4 identity halt: out-of-shard membership is VOID by contract (strip, counted); out-of-enum referent_kind quarantines the group (keep item); both responses cached → replay $0

**Verified on real artifacts:**
- `groups[17] duplicate atom_m_wh_ch04_b044_01`: surface = "Hindley" @b044 ("Hindley put out his tongue"). Shard 2 (which OWNS the atom — scaffold shard files verified: shard_01 does NOT contain it, shard_02 does) assigned it to ent_hindley: CORRECT. Shard 1's young-Heathcliff group (grp3, 14 members, evidence = the colt-exchange speech) listed it as a MEMBER despite it never being in shard 1's input — the model fabricated a plausible atom id from quote-context (the inverse face of the redundant-ID pathology). 
- `groups[9] referent_kind invalid:family`: 2-atom group (b019/b023), status already uncertain — semantically a household/group reference, labeled outside the enum.

**Rules (validator/apply-side only; prompt LOCKED; both ch4 identity responses cached → resume replays at $0):**
1. **Shard jurisdiction:** `member_atom_ids` must be a subset of the shard's input atom set. A member outside it is void ab initio — the model was never given assignment authority over that atom; enforcing this is contract enforcement, not an identity judgment. Strip the foreign member (counter `member_out_of_shard_stripped`, expect 1), KEEP any evidence citation of that atom (evidence legitimately crosses groups AND shards — amendment #2 extended: evidence source atoms validate against the full as-of atom catalog). After stripping, the global exact-partition check must hold via the owning shard's assignment; if the owning shard did NOT claim the atom, partition fails → halt as today (stripping into a void is not allowed).
2. **Out-of-enum referent_kind:** keep the group, force review_only quarantine, preserve the raw kind string verbatim on the record (`referent_kind_raw`), counter `referent_kind_out_of_enum` (expect 1). NO silent ontology mapping by code (family→group_reference is probably right — that's the reviewer's one-click decision, not code's).

**Tests:** real ch4 shard fixtures pass end-to-end (strip → partition exact; family group lands in review_only with kind preserved); synthetic foreign-member-unclaimed-by-owner → halt; synthetic out-of-enum kind on a resolved person group → still quarantined.

**Acceptance observations recorded for the ch1–4 gate (NOT part of this halt):**
- Shard 1 grp3 = young Heathcliff in Nelly's tale, reuse=None → will MINT a second Heathcliff entity (ent_heathcliff) alongside ent_mr_heathcliff. This is the predicted "Heathcliff-dup" case (schema-freeze memory) arriving on schedule: code must NOT overrule reuse=None via hints (that would be the M4c sin in reverse). Let it mint; measure at the gate; it is the strongest single piece of evidence for the frame-layer agenda item (story-time vs block-time) in the Sol dual review.
- b044 quote used as same_identity evidence across shards = amendment #2's cross-group evidence working as designed at shard scale.

---

## PRE-SCALE AGENDA (2026-07-11, user-locked) — Consolidation-unit abstraction: bàn với Sol + implement TRƯỚC khi scale WH 34ch

User chốt: "bàn luận và thực hiện trước những gì nó ảnh hưởng để không phải quay lại rerun". Full design note: memory `consolidation-unit-token-budget-design`. Tóm tắt cho vòng Sol:
- Measured wall: WH ch9/ch10/ch17/ch21 = 129/103/93/119 blocks (8.2–9.4k tok) — hits BEFORE Gatsby (Gatsby ch1 = 153 blocks/8.1k; ch7 = 416/11.9k). Ch3 (67 blocks) already forced 2 identity shards + output cap raise.
- Memory time-axis is the BLOCK STREAM (valid_from/to_block); "chapter" is only the consolidation cadence → replace `chapter_id` with `unit_id` in the chain (chapter becomes metadata). Answers the advisor's no-chapter question systemically.
- Cut ladder: author chapter > scene break (typographic only) > block boundary; NEVER mid-block. Budget decides WHEN, structure decides WHERE (cut position quality = measured under-merge cost: ghost-Catherine/two-Jabez).
- Budget is PER-MODEL and MEASURED, not guessed (mini degrades earlier than 5.4 — window-500 lesson; ~4–6k/unit for 5.4 digest is a hypothesis to verify at scale). Deterministic + config-hashed.
- Affected layers: ONLY B0 + M2 swallow whole chapters (M1 windowed, M3 sharded already).
- Sequencing: finish ch1–4 pilot + dual review AS LOCKED → Sol spec round for unit abstraction (chain migration, budget measurement plan) → implement → THEN 34-ch scale; Gatsby probe rides on it (ch1 as 2–3 units tests cross-unit machinery inside one chapter).

---

## AMENDMENT #10 (2026-07-11, Claude gate) — phase ch4 reject: implement the LOCKED pair-level quarantine (design §5.4), conservative branch; misquote is a REAL model slip, correctly not repaired

**Verified on artifacts:** row[6] pair (ent_mr_heathcliff, ent_the_master) friendly b038→b040, trigger_evidence "take it home with me"; source b038 reads "take it home with him at once" — Nelly's REPORTED speech transposed by the model into first person. Same event, different words → lexical deviation, NOT mechanically repairable (choosing the span the model "meant" would be a judgment; punct-fold correctly refused). The pair's other row [7] (dependent b041→open, "He took to Heathcliff strangely") validates fine. The remaining 8 rows across 7 pairs look clean.

**Decision — implement what design §5.4 already specifies (currently the code halts the whole run on any phase reject):**
1. Partition response rows by pair. Pairs whose rows ALL validate → publish. Any pair with ≥1 semantically rejected row → quarantine the ENTIRE pair as `blocked_for_runtime` (publishing only its valid rows would fabricate a timeline missing its first beat — the design's "single-phase CHE under-seg" warning). Counted in `#pairs_blocked_for_runtime` (§5.7 metric, already specified). Quarantine record carries ALL the pair's returned rows + reject reasons for human review.
2. **Conservative deviation, documented:** design permits a single-phase FALLBACK for simple pairs; we do NOT implement it now — a fallback label choice has no mechanical source (observed_valence_hint→label is a judgment). Uniformly blocked_for_runtime until a real case justifies designing a mechanical fallback (measure first).
3. Prior state protection: a blocked pair's timeline from earlier scopes is RETAINED unchanged and marked needs-review — a rejected response must never delete published history. (This pair has no prior state; rule is for generality.)
4. Facts referencing a blocked pair → same quarantine (none in ch4: model returned zero relation_facts).
5. Run halts only if ALL pairs reject, or on technical failure — unchanged otherwise.
6. Prompt LOCKED untouched → the phase response is cached; resume replays at $0. Expected ch4 outcome: 8 phases published across 7 pairs, `pairs_blocked_for_runtime=1`, publish + checkpoint proceed.

**Tests:** real ch4 raw as fixture → publish with exactly pairs_blocked_for_runtime=1 and 8 phases; synthetic all-pairs-rejected → halt; synthetic pair-with-prior-history rejected → prior timeline retained + flagged; blocked pair excluded from runtime queries/address-policy.

**Acceptance observations (recorded for the ch1–4 gate + dual review, NOT this halt):**
- Old-Earnshaw fragmentation is now THREE-way: ent_t_maister (ch3) + ent_the_master + ent_earnshaw (both ch4) — cross-chapter under-merge multiplying on the same person; headline under-merge item for the Sol review + Auditor (§5.6) design. Partition direction vs Heathcliff/Hindley still CORRECT (the-master criterion counts separation, and separation held).
- ch4 relation_facts = [] despite parent_of evidence in Nelly's tale (model ultra-conservative under "no fact from title/honorific alone") — recall observation.
- New failure mode catalogued: reported-speech→first-person transposition in "verbatim" quotes — quote discipline slip class for the review list and any future prompt v2 (post-pilot).
