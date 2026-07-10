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
