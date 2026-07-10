# TASK_LIT_M4c — Acceptance C + M4 full (ch1-4) + estimator cap toàn sách

Task CHẠY + BÁO CÁO thuần theo trình tự khoá sẵn. KHÔNG sửa code, KHÔNG sửa prompt/design doc,
KHÔNG tự vá khi gặp lỗi — dừng và báo. Mọi kết quả Claude sẽ verify lại trên artifact.

## Bước 0 — Estimator 0-API toàn sách (KHÔNG gọi API)
Chạy estimate_m1 + estimate_m2 cho TOÀN BỘ 34 chương WH (từng chương một), gom:
- max/percentile prompt-token ước tính THEO MODE (B0 brief / B1 lexicon / B2 narrative / B3 digest).
- Bảng chương nào vượt 6500 (nếu có).
Mục đích: (a) chọn cap cho run M4 này = max(estimate ch1-4) + ~10-15%; (b) số liệu để Claude + user
chốt cap trước scale 34 chương (KHÔNG tự chốt cap toàn sách trong task này).
Nếu ch1-4 cần cap > 6500: tạo config MỚI `llm_prepass_m4full.yaml` (copy m4a, chỉ đổi
prompt_token_cap theo số đo, ghi rõ deviation). Nếu 6500 đủ: dùng nguyên llm_prepass_m4a.yaml.

## Bước 1 — Acceptance C: replay ch1-2 mint checkpoint (~$0)
- run_m1 chapters=[wh_ch01, wh_ch02], out_dir MỚI `data/reports/literary_m4_full/`,
  **DÙNG LẠI cache file của run m4a** + đúng config (temp/seed/model y nguyên) → kỳ vọng cache-hit
  toàn bộ, this_attempt ≈ $0 (trần chấp nhận $0.10 nếu lác đác miss).
- Báo: accounting_resume (this_attempt phải tách rõ), checkpoints/m1/wh_ch01.json + wh_ch02.json
  tồn tại, và entity_ledger checkpoint ch2 KHỚP canonical với m1_report của m4a đã gate.

## Bước 2 — M4 full M1: resume chạy ch3-4 (~$0.10-0.15 API thật)
- run_m1 chapters=[wh_ch01..wh_ch04] --resume, CÙNG out_dir.
- Báo: resume.resumed_from_checkpoint == [ch1,ch2], resume.ran == [ch3,ch4]; accounting 3 phần
  (restored/this_attempt/combined — KHÔNG trình bày cả chuỗi như "$0"); retry rate ch3-4 (kỳ vọng
  ~2-5%); counters validator; token thật.

## Bước 3 — M4 full M2: B3 digest ch1-4 (vài cent)
- run_m2 chapters=[wh_ch01..wh_ch04], m1_dir = out_dir trên (checkpoint đã có).
- Báo BẰNG CHỨNG as-of trên dữ liệu thật: với MỖI chương, trích dòng CHAPTER_ROSTER trong digest
  prompt thật; digest ch1 KHÔNG được chứa entity xuất hiện lần đầu ch2+ (vd Zillah, Mrs. Heathcliff);
  digest ch2 không chứa entity mới của ch3-4.
- Báo: 4 digest ok, chapter_rolling_summary từng chương, m2 checkpoint có input_m1_checkpoint_hash.

## Bước 4 — M4 full M3: B4 Story Bible (0-API)
- run_m3 ch1-4 từ m1_dir + digest_dir trên.
- Báo THÔ (không tự đánh giá đúng/sai): story_bible files + đường dẫn; danh sách entity sau merge;
  aliases + valid ranges; phases từng cặp (đặc biệt Lockwood–Heathcliff, Lockwood–Mrs. Heathcliff);
  address_policies; narration_frame_segments ch1-4 (ch4 phải có ≥2 segment nếu code đúng);
  số phận các ca: ent_heathcliff vs ent_mr_heathcliff, ent_hareton vs ent_hareton_earnshaw,
  ent_king_lear, ent_mr_and_mrs_heathcliff. ĐÂY LÀ INPUT GATE B4 CỦA CLAUDE — chỉ liệt kê sự thật.

## Ràng buộc chung
- OPENAI-KEY-2; xoá key khỏi env sau chạy; key-scan report trước khi nộp.
- Frozen D2L DB hash trước/sau phải = 64D989...C715. Design doc không diff. git status sạch ngoài
  out_dir mới + (nếu có) config m4full.
- Bất kỳ bước nào lệch kỳ vọng (cache miss hàng loạt, retry >10%, resume không skip, as-of lộ
  entity tương lai): DỪNG ngay tại bước đó, báo cáo, không chạy tiếp.

---
## AMENDMENT rev2 (Claude, 2026-07-10) — resolve điểm dừng bước 0

**Lỗi spec (của Claude):** M2 estimator cần m1_report/checkpoint tồn tại → "estimate M2 toàn 34
chương" là bất khả thi trước khi M1 toàn sách chạy. CodeX dừng đúng luật.

**Resolution — số liệu M1 đã verify trên artifact, đủ để quyết:**
1. BỎ yêu cầu estimate M2 toàn sách khỏi bước 0. Proxy cấu trúc: mọi chương vượt cap đều do B0
   (đọc trọn chương; max 10,784 @ ch17; B1/B2 max chỉ 3,780/4,072 — bounded đúng thiết kế);
   B3 digest cùng lớp whole-chapter như B0. Estimate M2 thật cho ch1-4 sẽ hiện ở confirm-gate
   của chính run_m2 tại bước 3 (đã đủ guard).
2. **Cap cho run M4 ch1-4 CHỐT = 9300** (max ch1-4 = 8,041 @ ch03, +15% ≈ 9,247 → 9,300).
   CodeX tạo `llm_prepass_m4full.yaml` = copy m4a, chỉ đổi prompt_token_cap: 9300. Dùng config
   này cho bước 1-3 (bước 1 replay: cache key KHÔNG phụ thuộc cap nên vẫn hit cache m4a).
3. Cap TOÀN SÁCH: chưa chốt — để pre-scale review. Khuyến nghị mang theo: tách CAP THEO MODE
   (chapter-modes B0/B3 ≈ 12,400 = 10,784+15%; window-modes B1/B2 ≈ 4,700 = 4,072+15%) — một cap
   chung 12k sẽ làm bẫy B1/B2 (4k) thành vô dụng.
4. Ghi chú cost: $11.12 là UPPER-BOUND CAP (max_output × giá list mọi call), không phải kỳ vọng;
   thực đo m4a ch1+2 = $0.095 → toàn sách kỳ vọng ~$1.6-2.0 mini.

**Lệnh tiếp:** chạy bước 1 → 4 như spec gốc với llm_prepass_m4full.yaml. Điều kiện dừng giữ nguyên.

---
## AMENDMENT rev3 (Claude, 2026-07-10) — cap cho M2 + bài học config_hash

Verify bước 1-2: PASS (resume/accounting/checkpoint khớp từng số; retry ch3-4 = 1.67%).
Đo mới: B3 digest = B0 + ~25% overhead (ch03: 10,063 vs 8,041) — proxy rev2 thiếu phần cộng.

**Quyết định cap M2:** tạo `llm_prepass_m4full_m2.yaml` = copy m4full, prompt_token_cap: **11,500**
(10,063 + ~14%). DÙNG RIÊNG cho run_m2. KHÔNG sửa llm_prepass_m4full.yaml.

**Lý do tách config (quan trọng):** prompt_token_cap nằm trong config_hash của checkpoint (spec M4b
— lỗi thiết kế của Claude: cap là GUARD, không ảnh hưởng output model, lẽ ra không thuộc hash).
Sửa cap trong config m4full sẽ INVALIDATE 4 checkpoint M1 vừa tạo → resume ch5+ sau này phải re-run
ch1-4. Tách config M2 riêng thì checkpoint M1 giữ nguyên hiệu lực.
**Ghi vào agenda pre-scale review:** loại prompt_token_cap/daily_token_cap/pricing khỏi config_hash
(chỉ giữ field ảnh hưởng output: model/temp/seed/reasoning/verbosity/response_format/
max_output_tokens/window/K) — kèm test; làm TRƯỚC scale 34 chương để đổi cap không phá checkpoint.

**Lệnh tiếp:** run_m2 ch1-4 với llm_prepass_m4full_m2.yaml → run_m3 → nộp báo cáo B4 thô như spec.
Điều kiện dừng giữ nguyên.

---
## AMENDMENT rev4 (Claude, 2026-07-10) — FIX D: narration coverage tính trên block TỰ SỰ

**Chẩn đoán (verify trên block text thật):** cả 3 retry M2 cùng một gốc — MODEL ĐÚNG, VALIDATOR SAI:
- ch01/ch02 b001 = block heading "CHAPTER I/II" (block_type=heading) — không có người kể;
  model bắt đầu segment ở b002 là đúng ngữ nghĩa.
- ch04 b034 = "* * * * *" (block ngăn cảnh, không một chữ cái) — Nelly bắt đầu ở b035, model đúng.
Validator ép narration_frame_segments phủ MỌI block → bắt model gán người kể cho heading/dấu sao.
Nguy hiểm hơn: attempt-2 "pass" là model ĐẦU HÀNG validator (khai láo b001 có narrator) — artifact
digest hiện tại đang chứa segment start sai ngữ nghĩa. Retry-halt 10% đã cứu B4 khỏi input bẩn.

**FIX D (validator-only, mechanical, KHÔNG đụng prompt):**
- Coverage domain của narration_frame_segments = block có block_type != "heading" VÀ text chứa
  ít nhất 1 chữ cái (letterless separator loại ra — kiểm tra cơ học, không phán đoán ngôn ngữ).
- Block bị loại → đếm `nonnarrative_block_skipped` + warning. Gap trên block TỰ SỰ thật vẫn HARD-FAIL.
- Bump M2_CHECKPOINT_SCHEMA_VERSION (validator đổi hành vi → checkpoint M2 cũ tự invalid; M1 giữ).
- Test: 3 case thật (heading đầu chương, separator giữa chương, gap thật trên block tự sự → fail).

**Verify + chạy lại (gần $0):**
1. 0-API replay: attempt-1 của 3 digest retry phải PASS validator mới, segment giữ nguyên b002/b035.
2. Re-run run_m2 ch1-4 CÙNG cache file → cache trả lại đúng attempt-1 cũ → được nhận, ~$0,
   digest artifact + checkpoint M2 mới mang segment ĐÚNG ngữ nghĩa (thay bản attempt-2 khai láo).
3. run_m3 → nộp báo cáo B4 thô như spec gốc.

---
## AMENDMENT rev5 (Claude, 2026-07-10) — mổ xẻ lệch estimate 10,063 → runtime 12,379: KHÔNG phải bug prompt

**Kết luận:** prompt runtime hợp lệ, KHÔNG có gì phình bất thường. Cái sai nằm ở ESTIMATOR, hai lỗi
hệ thống (đều đã đo trực tiếp trên code/artifact):
1. `estimate_prompt_tokens` = chars//4 (llm_client.py:327) — văn xuôi văn học (em-dash, quote dày)
   tokenize > 1 token/4 chars → thiếu hệ thống ~15% (~1.5-1.9k trên prompt 10k).
2. estimate_m2 điền neighbor summary bằng PLACEHOLDER 8 token "(generated by this chapter digest…)"
   trong khi runtime mang 2 rolling summary THẬT (~200 token × 2) → thiếu ~0.4k.
Cộng hai khoản ≈ +2.3k = đúng khoảng lệch quan sát. Thành phần prompt digest ch03 thật: chương trọn
(chi phối) + brief 1.5k chars + roster as-of 31 entity 1.4k chars + relation events 4.5k chars
+ 2 neighbor thật + system prompt — tất cả đúng thiết kế.

**Có cắt gì không? KHÔNG cắt bây giờ.** B3 cần nguyên chương + evidence quotes để cắm cờ chuyển pha;
digest chỉ 1 call/chương nên không phải vector chi phí. HAI WATCH-ITEM cho pre-scale (không làm giờ):
- CHAPTER_ROSTER = full ledger as-of, LỚN DẦN theo sách (31 @ch03 → có thể 3× @ch34) — nếu phình
  quá thì cap roster về entity xuất hiện trong chương + main cast (quyết bằng số đo M4/scale).
- relation events mang full quote — có thể truncate NHƯNG là đổi prompt, cần gate riêng.

**Quyết định:**
1. Cap M2 nâng theo SỐ RUNTIME: 12,379 × ~1.13 ≈ **14,000**. Sửa trong llm_prepass_m4full_m2.yaml
   (config M2 riêng — không đụng M1/checkpoint).
2. Fix estimator (0-API, không ảnh hưởng checkpoint): estimate_m2 dùng placeholder neighbor thực tế
   (~150 từ); ghi chú calibration chars//4 → sau M4 có 4 số runtime B3 thật để tính hệ số est→runtime
   cho quyết định cap toàn sách ở pre-scale (KHÔNG chốt cap 34 chương bằng estimate thô).
3. Chạy tiếp: re-run run_m2 ch1-4 (cache: ch1-2 hit; ch03-04 API mới ~$0.03-0.05) → run_m3 → báo cáo
   B4 thô. Điều kiện dừng giữ nguyên.

---
## AMENDMENT rev6 (Claude, 2026-07-10) — ĐÍNH CHÍNH rev5 + quyết định model M2

**Đính chính rev5 (dữ liệu tra hỏi per-call):** 12,379 = PROMPT RETRY (prompt gốc + JSON attempt-1
~2k + lỗi validator), không phải prompt gốc. Prompt gốc ch03 = 10,546 vs estimate 10,063 = +4.8%,
gần hết do neighbor placeholder (~400 tok). chars//4 gần đúng (bias ~+1%), KHÔNG phải thiếu 15%
như rev5 viết. Công thức cap đúng cho M2: max_initial_prompt + ~2.5-3k retry-overhead.

**Cap M2 pilot: GIỮ 14,000** (12,379 đo được + 13%). Từ chối 16-18k "để dành chỗ" — cap là dây bẫy
đo được, không phải chỗ trú cho bug tương lai. Cap toàn sách: chốt ở pre-scale bằng công thức trên
với max_initial đo từ M1 artifact thật.

**Model cho M2 — quyết định:**
- KHÔNG nâng reasoning (đã đo và khoá: reasoning>none ép temp=1.0 trên mini, ăn output budget,
  không sửa được schema slip — xem reasoning-effort memory).
- Mini GIỮ cho M1 (hàng trăm call, validator scaffolding đã gánh tốt, 1.67% retry).
- M2/B3 = tầng đòn bẩy cao nhất pipeline: chỉ 34 call/sách nhưng output là TRÍ NHỚ mà chương sau
  và B4 tiêu thụ — lỗi ở đây lan truyền. Kinh tế học nghiêng về model mạnh: 34 digest × ~13k tok
  ≈ 450k tok ≈ 2 ngày quota free gpt-5.4 hoặc ~$2-3 trả tiền.
- Nhưng KHÔNG đổi theo cảm giác: **A/B pre-registered** — cùng M1 artifact, chạy digest ch03 VÀ
  ch04 (2 chương để tránh tune trên đúng chương chẩn đoán), 2 arm: gpt-5.4-mini vs gpt-5.4, cùng
  reasoning=none, cùng cap 14k, cache riêng. Metric chốt TRƯỚC: (a) first-pass validity;
  (b) coverage nội dung digest (frame segments đúng ranh giới đã biết b002/b035; state_changes/
  threads/motifs đếm + đối chiếu tay); (c) chất lượng rolling summary (Claude đọc gate);
  (d) token/chi phí. Thắng rõ → gpt-5.4 cho M2 TOÀN BỘ 34 chương (đồng nhất theo STAGE, ghi
  provenance); không rõ → mini giữ nguyên.
- Nguyên tắc đồng nhất: model cố định THEO STAGE trên toàn sách (M1 mini, M2 = kết quả A/B, M3 code)
  — cấm đổi model giữa sách trong cùng stage. Không cần một model cho mọi stage.

**Lệnh tiếp:** (1) cap m4full_m2 → 14,000; (2) A/B digest ch03+ch04 mini-vs-5.4 theo thiết kế trên
(5.4 dùng OPENAI-KEY-2 quota free, config gpt54 riêng); (3) nộp kết quả A/B cho Claude gate chọn
model M2; (4) SAU khi chốt model mới re-run M2 đủ 4 chương + run_m3 + báo cáo B4 thô.

---
## AMENDMENT rev7 (Claude, 2026-07-10) — M2 as-of SUBSET selection (fix blocker A/B + lỗ hổng neighbor)

**Blocker CodeX báo: ĐÚNG.** Chain-validation của M2 tính expected (chapter_index/prefix/parent) từ
DANH SÁCH ĐƯỢC CHỌN của M2 thay vì từ chuỗi tuyệt đối M1 đã chạy → chọn suffix ch03-04 fail.

**Lỗ hổng THỨ HAI (Claude soi thêm, nghiêm trọng hơn cho A/B):** neighbor summaries chỉ tích lũy
trong vòng lặp của chính run — chọn [ch03,ch04] thì ch03 nhận neighbor "(none)", KHÁC prompt
production (ch03 phải có summary thật của ch01-02). Và arm B (config gpt-5.4) không đi được đường
--resume vì config_hash khác → invalidate checkpoint M2 mini → re-run ch01-02 bằng 5.4 = confound
(neighbor hai arm khác nhau + tốn token). A/B mà chạy theo spec rev6 hiện tại sẽ đo sai prompt.

**Thiết kế fix (code, validity-touching — đề nghị Terra xhigh implement):**
1. **Chain validation tuyệt đối:** expected của checkpoint M1 chương X tính từ
   `m1_report.chapters_selected` (chuỗi M1 THẬT SỰ đã chạy): chapter_index = vị trí trong chuỗi đó,
   prefix = chuỗi tới X, parent = checkpoint chương liền trước trong chuỗi đó. Validate TOÀN BỘ
   chuỗi tổ tiên từ chương đầu đến X (mọi ancestor phải tồn tại + hash nối liền), bất kể M2 chọn gì.
2. **Neighbor = DATA INPUT, không phụ thuộc config:** với chương ở vị trí tuyệt đối i, neighbor k=2
   = digest summary của i-1, i-2 lấy theo thứ tự ưu tiên: (a) kết quả in-run nếu chương đó nằm trong
   selection; (b) đọc từ `--digest-context <dir>` (digest artifact/M2 checkpoint có sẵn trên disk —
   chấp nhận bất kể config_hash vì đây là INPUT, ghi provenance `neighbor_source` vào report).
   Mặc định STRICT: thiếu digest neighbor → hard-fail (không im lặng "(none)").
3. estimate_m2 đi cùng đường mới. Test: (a) suffix ch03-04 validate pass với chain đầy đủ;
   (b) ancestor đứt → fail; (c) neighbor từ context-dir BYTE-EQUAL neighbor in-loop (so canonical,
   theo pattern crash-sim); (d) thiếu neighbor digest → fail rõ ràng.
4. **A/B sau fix — hai arm ĐỐI XỨNG tuyệt đối:** cùng `run_m2 --chapters wh_ch03,wh_ch04
   --digest-context data/reports/literary_m4_full`, out_dir RIÊNG mỗi arm, chỉ khác config model.
   Neighbor hai arm = cùng bản mini ch01-02 đã có → chỉ MỘT biến (model).

**Ghi chú:** feature này không phải đường vòng — nó chính là hạ tầng scale cần cho ch5+ (digest
tiếp không re-digest 4 chương đầu). Điều kiện dừng giữ nguyên.

---
## GATE rev7 (Claude verify độc lập, 2026-07-10): **PASS**
- Tự chạy: 4/4 subset test + 359/359 full suite. Design doc không diff. Scope đúng 4 file + configs.
- Đọc code: chain tuyệt đối validate MỌI tổ tiên tới chương xa nhất được chọn (parent-hash liền,
  prefix tuyệt đối, dup-check, document-consistency, config_hash nhất quán NỘI-CHUỖI — đúng ngữ
  nghĩa: config M1 độc lập config M2 hiện tại); legacy single-chapter guard giữ nguyên; neighbor
  resolver fail-closed, provenance per-neighbor, in-run summary rỗng cũng fail. Preflight 0-API:
  2 call/arm, max 10,274 < 14,000.
→ DUYỆT CHẠY A/B ch03+ch04 hai arm đối xứng (mini vs gpt-5.4, cùng --digest-context
literary_m4_full, out_dir riêng, cache riêng). Chi phí: arm mini ~$0.01; arm 5.4 trong quota free.
Nộp per-arm: first-pass validity, narration segments (b002/b035), đếm state_changes/threads/motifs,
rolling summary NGUYÊN VĂN 2 chương, token/cost. KHÔNG tự chọn model.
Commit: gộp MỘT batch sau khi Claude chốt model M2 (FIX D + rev7 + configs + A/B artifacts + verdict).

---
## GATE A/B MODEL M2 (Claude, 2026-07-10): **gpt-5.4 THẮNG — chốt cho toàn stage M2**

Chấm theo metric pre-registered rev6, verify trên artifact:
- (a) First-pass validity: HÒA (2/2, 0 retry cả hai). (b) Ranh giới b002/b035: HÒA (đúng cả hai).
- (c) Nội dung: 5.4 THẮNG RÕ. Bằng chứng quyết định: state_change duy nhất của mini ch03 là RÁC
  ("sleeping"→"awake_and_shaken" nhét vào life_status — lạm dụng schema); 5.4 trả 0 state cho ch03
  = ĐÚNG (chương một đêm, không có lifecycle change thật). Threads 5.4 sắc + neo evidence hơn
  (lịch sử căn phòng cấm, bond Heathcliff–Catherine qua lời gọi "Cathy", hostility Heathcliff–
  Mrs. Heathcliff). 
- (d) Rolling summary — trục ăn tiền nhất vì đây là TRÍ NHỚ nuôi chương sau + B4: 5.4 giữ được các
  memory item tải trọng: nội dung nhật ký (Hindley tàn ác — evidence pha quan hệ), giấc mơ Jabez,
  ghost child "Catherine Linton", Heathcliff cầu xin "Cathy" quay lại, cảnh Zillah/daughter-in-law;
  ch04: CÁC IDENTITY FACT (Mrs. Heathcliff = Catherine Linton; Hareton = cousin, Earnshaw cuối) —
  đúng thứ B4 cần dựng quan hệ gia đình. Mini bỏ sạch identity facts, summary generic hơn.
- Cost: mini $0.0106/2ch; 5.4 = 25,131 tok quota free (toàn sách ~425k ≈ 2 ngày quota — chấp nhận).

**Quyết định:** M2/B3 = gpt-5.4 (reasoning none, config m4full_m2_gpt54) cho TOÀN BỘ 34 chương,
provenance ghi model per-stage. M1 giữ mini. Đồng nhất theo stage, không đổi giữa sách.
**Lệnh tiếp:** re-run M2 ch01-04 TOÀN BỘ bằng 5.4 (~50k tok quota, digest ch01-02 mini bị thay —
stage phải thuần nhất) → run_m3 → báo cáo B4 thô. Xác nhận luôn trạng thái fix estimator rev5
(neighbor placeholder ~150 từ) — nếu chưa làm thì làm trong lượt này.

---
## GATE M4c CUỐI (Claude verify độc lập trên artifact, 2026-07-11): **M2 PASS / B4-pilot FAIL (hết tầm đúng dự kiến)**

### M2 gpt-5.4 ch01-04: **PASS — chốt stage**
- Verify từng số trên `m2_report.json`: 4/4 call attempts=1, per-chapter prompt/completion khớp
  từng token với báo cáo (4717/1674, 8800/3357, 10670/3247, 7645/3348; tổng 31832+11626=43458).
- Model đồng nhất `gpt-5.4`; neighbor provenance `in_run` đúng cả ch2-4; resume accounting sạch.
- Digest ch02 mang đủ evidence tải trọng: "Mrs. Heathcliff" ×40, daughter-in-law b042/b046 → input
  cho B4 là ĐỦ, lỗi phía dưới không phải lỗi M2.
- Estimator rev5: diff chỉ chạm đường estimate (placeholder ~150 từ, provenance
  `in_run_estimate_placeholder`, đường chạy thật không đổi) + 1 test mới. Đúng spec.
- An ninh: frozen DB hash 64D989...C715 tự tính lại KHỚP; grep `sk-` sạch trên artifacts mới.

### B4/M3: **FAIL acceptance — nhưng đúng nghĩa "pilot ch1 hết tầm", không phải lỗi thi công**
Chấm 5 acceptance check trên `story_bible/*.json` thật:
1. **Heathcliff dup merge: FAIL (tệ hơn không merge).** `ent_mr_heathcliff` gộp đúng vào
   `ent_heathcliff` NHƯNG rule `honorific_variant_same_surface_core` gộp luôn
   `ent_mrs_heathcliff` — Mrs. Heathcliff là CON DÂU (Catherine trẻ), nhân vật khác hoàn toàn.
   `_strip_leading_honorific` (mr|mrs|miss cùng lõi họ → 1 cluster) là code làm identity judgment
   = việc ngôn ngữ. Đúng tình cờ với ch1, sai hệ thống từ ch2 (cả ent_mrs_earnshaw cũng bị nuốt).
2. **Hareton ch1↔ch2 join: FAIL.** `ent_hareton` / `ent_hareton_earnshaw` vẫn 2 entity; canary
   `hareton_historical_not_present=false` bắt đúng.
3. **King Lear allusion: PASS.** `presence_status=mentioned_historical`, không vào runtime role.
4. **Narrator switch ch4: PASS.** 2 segments (Lockwood b002-b033 frame_present; Mrs. Dean
   b035-b045 retrospective_past), separator b034 exempt đúng theo FIX D.
5. **Daughter-in-law phase: FAIL.** Evidence có đủ trong digest nhưng merge sai xoá nhân vật →
   không tồn tại phase nào cho bà; quan hệ (heathcliff, mrs_heathcliff) biến mất.

Lỗi cấu trúc thêm (đều do M3 hiện tại = code pilot ch1 chạy lặp 4 lần):
- **Future-leak as-of tầng M3**: cả 4 file bible đều chứa nguyên ledger CUỐI 30 entity (bible
  "as-of ch1" biết Hindley/Zillah/Catherine Linton/Nelly) — đúng lớp bug M2 as-of vừa diệt.
  Scope hardcode `"ch1"`/`"M3_ch1"`, canary ch1-specific chạy trên data 4 chương.
- Pair không chuẩn hoá unordered: (lockwood,heathcliff) ≠ (heathcliff,lockwood) thành 2 phase;
  (heathcliff,hareton) trùng 2 interval cùng mở. Mọi interval `valid_to=null` — không có
  change-point, chưa phải interval-valued memory thật.
- Phase engine = `generic_valence_fallback` toàn "strained" + needs_human_review (dán nhãn
  trung thực, nhưng là placeholder). `entity_type` hardcode "person" (Wuthering Heights = person).
- Fragment chưa xử: ent_catherine_s / ent_heathcliff_s / ent_jabez_s / ent_your_servant_zillah /
  ent_and_mrs_heathcliff (surface garbled).

Điểm giữ đúng kỷ luật: address_policies chỉ phát observed vocatives + runtime_usable=false +
proposal_only (đúng code-never-does-language-work); halt đúng chỗ (`needs_claude_gate`); artifact
tự khai `partial_story_bible`, canary fail không giấu.

**Quyết định:** M2 đóng. B4 v2 = task thiết kế riêng (TASK_LIT_M4d) — 2 vòng thảo luận CodeX
(Sol xhigh review) trước khi implement; KHÔNG scale 34 chương trước khi B4 v2 pass gate trên ch1-4.
