# TASK_EV_D2L_06_memory_tradeoff_judge — MEMORY_TRADEOFF_01: định lượng chi phí chất lượng của memory trên **59 term override** (S0 nhất quán → S1 đổi form), **judge MÙ** (Gemini ngây-thơ + người neo), **post-hoc trên frozen output**, KHÔNG re-translate / KHÔNG re-run Builder

- **Status:** READY (Claude, 2026-06-19) — chờ CodeX điền §5 + REVIEW; KHÔNG commit.
- **Refs:** memory `d2l-scorer-validity-and-remediation-ladder`, `dont-tune-intervention-on-test-baseline`, `scoring-scope-equals-production-scope`, `green-tests-can-hide-dead-integration` · THESIS_ARCHITECTURE_LOCK §10 (uu)/(vv)/(ww) · EV_D2L_03b (`pipeline/eval/surface_match.py`) · EV_D2L_05 (`pipeline/eval/occ_align.py` — `build_occ_frame`) · EV-02 (judge Gemini infra, pairwise A/B, proxy ShopAIKey) · RunControl cost-gate (APP-C01) · 3-bên hội tụ Claude+GLM-5.2+user 2026-06-19.
- **Branch/Commit:** local working tree only; not committed per task protocol.

## 1. Bối cảnh & mục tiêu *(Claude viết)*

**Phát hiện nền (đo được, frozen output):** memory (glossary injection, config S1) **ĐÈ** một rendering mà S0 vốn đã nhất quán ở **59 term**. Spot-check tay thấy nhiều ca **ngang hoặc tệ hơn**, KHÔNG phải cải thiện:

| Term | tier | S0 (đã nhất quán) | → S1 (memory ép) | Spot-check |
|---|---|---|---|---|
| rules | ignore | quy tắc | **luật** | SAI nghĩa (ngữ cảnh toán/ML) |
| framework | soft | framework | **khung phần mềm** | ép dịch, kém tự nhiên |
| batch size | hard | kích thước minibatch | kích thước lô | đổi vô ích |
| MNIST dataset | preserve | bộ dữ liệu MNIST | **MNIST** | rụng chữ |
| model's parameters | hard | tham số của mô hình | tham số mô hình | chỉ rụng "của" (vô hại) |

**Câu hỏi pilot:** trong 59 override, bao nhiêu % là **improve / lateral / harm** (adequacy so với nghĩa NGUỒN EN)? → định lượng **chi phí precision của memory**, biến trực giác "memory không phải lúc nào cũng tốt" thành con số.

**Tại sao đây là đóng góp, không phải lỗi:** finding "memory là đánh-đổi" (giúp ~25% drift-prone, đè ~15% với X% harm) **mạnh hơn** claim "memory luôn tốt". 3 keystone evidence: `set→bộ` (điểm mù registry, EV-05 b003), `rules→luật`, và **59 override** này. Đây là VỐN của luận văn — đo, không đập.

**Cốt lõi phương pháp (user nêu, đúng — và là phần nặng nhất của task):** Claude/CodeX/GLM **không tin được** làm judge (đã dự tranh luận, biết đáp án); chính tác giả tự chấm cũng **phải mù**. Một con số adequacy chỉ đáng trình hội đồng khi judge: **(1) họ ngây-thơ** (Gemini, chưa hề dự) **+ (2) mù xuất xứ** (không biết đâu S0/S1/đâu form-glossary) **+ (3) đảo vị trí** (chống position bias) **+ (4) có nguồn EN + ngữ cảnh** (adequacy vs nghĩa nguồn, không chỉ trôi chảy) **+ (5) cho phép hòa** (không ép thắng) **+ (6) neo người, mù** (validate chính judge tự động). Thiếu một, hội đồng bẻ được.

**Phân biệt 2 trục (đừng nhầm như draft đầu của Claude):**
- **0-API CHỈ cho `consistency_status`** = "form S1 ≠ form S0" = **drift**. KHÔNG phải harm.
- **`improve/lateral/harm` LÀ phán adequacy** → **BẮT BUỘC** qua judge (LLM-judge + người), KHÔNG thể 0-API. (GLM push đúng; Claude nhận đã viết ẩu.)

**Đây là DIAGNOSTIC, không phải gate.** N=59 nhỏ; solo annotator → KHÔNG κ → occurrence-level vẫn `diagnostic_only` (nối L11 EV-05). Finding vẫn có giá trị luận văn kèm caveat methodology trung thực.

**Hậu kỳ tuyệt đối:** đọc frozen `translation_runs` + nguồn EN + report D-scorer. **KHÔNG re-translate, KHÔNG re-run Builder, KHÔNG lọc injection từ S0 test** (`dont-tune-intervention-on-test-baseline`: 59 override để BÁO CÁO, CẤM dùng để prune headline S1).

## 2. Scope

**IN (pilot, chương `preliminaries` + mọi term scored 4 chương theo report):**
1. **Tập override (suy ra tất định từ frozen report):** `build_override_set` đọc `d2l_translation_metrics_v2.json` (`forms_used`{surface→count} + `status` per term per config) → chọn term mà **S0 nhất quán** (`status_S0=='consistent'`) **VÀ** `S1_dominant_surface != S0_dominant_surface`. Ghi tier, 2 dominant form, occ đại diện, câu nguồn EN. Kỳ vọng ≈ **59** {hard 36 / soft 20 / preserve 2 / ignore 1} (cross-check từ phiên Claude; **luật = rule là source of truth, 59 là kiểm tra chéo** — lệch thì §5 đối soát, KHÔNG ép về 59).
2. **Worksheet MÙ (blind tại khâu SINH, không phải hiển thị):** `build_judge_worksheet` (seed=42) → 3 nơi tách biệt: `worksheet.jsonl` (mù, máy+người dùng) · `worksheet.html` (mù, người chấm) · `KEY/worksheet_KEY.json` (chỉ scorer đọc). Mỗi item: 1 occ đại diện/term → `en_sentence`, `en_term`, `version_A`, `version_B` (window VI quanh term, **đánh dấu «...»**), A/B = hoán vị ngẫu nhiên {S0-window, S1-window} theo seed. **Không trường nào lộ S0/S1.**
3. **Judge Gemini (họ ngây-thơ, mù, đảo, cost-gate):** `memory_tradeoff_judge` — mỗi item gọi **2 lần đảo chỗ** (A=S0/B=S1 và A=S1/B=S0); model = Gemini (**KHÔNG** gpt-5.4 = họ Translator; **KHÔNG** Claude/GLM); pin model+temp=0+seed; prompt term-focused reference-aware (§3); nhãn {A_better, B_better, equivalent}.
4. **Neo NGƯỜI, mù:** tác giả mở `worksheet.html`, chấm **subset stratified 12–15** (rải tier hard/soft/preserve/ignore), 1 hướng/item, export `judgments_human.json` — **cùng A/B mù** như Gemini.
5. **Chấm + báo cáo:** `score_memory_tradeoff` đọc KEY (có safeguard) + 2 judgment → `data/reports/memory_tradeoff_59_overrides.json`: per-term `{consistency_status (0-API), gemini_run1, gemini_run2, gemini_resolved, human_label?, final_label}` + tổng hợp `{improve%, lateral%, harm%}` + agreement người↔Gemini trên phần giao + caveat methodology.

**OUT (CẤM):**
- **RE-TRANSLATE / re-run Builder / đổi prompt dịch / ghi DB frozen.** Dùng `translation_runs` + report có sẵn.
- **Lọc/prune injection S1 dựa trên S0 test** (kể cả "skip inject nơi S0 dominant>90%") — `dont-tune-intervention-on-test-baseline`. 59 override CHỈ để báo cáo; sửa hệ (nếu cần) = **arm `S1_better` a-priori RIÊNG**, task khác.
- **Judge = họ Translator (gpt-5.4) / Claude / GLM** — chung lỗi với Translator hoặc đã biết đáp án → thiên lệch. Phải họ ngây-thơ.
- **Judge thấy xuất xứ** (S0/S1/form-glossary) hoặc thứ tự cố định không đảo → position/provenance bias.
- **Dùng case THẬT trong 59 làm ví dụ trong prompt template / spec** → spec public → người đọc trước khi chấm → **mù vỡ**. Prompt PHẢI dùng **dummy** (vd term `network`) ngoài 59.
- **Mở `KEY/` trước khi chấm xong** → mù vỡ silently. KEY ở folder riêng + README "DO NOT OPEN"; scorer chỉ đọc KEY khi cả 2 judgment đã đủ.
- **Force-choice (bỏ "equivalent")** → thổi phồng khác biệt.
- **Coi N=59 / solo là headline** — diagnostic_only; KHÔNG κ (cần 2 người); ghi caveat.
- **0-API rồi gọi là "harm"** — harm cần judge; 0-API chỉ ra drift.
- **Tiêu API ngầm** — Gemini qua cost-gate + confirm-token + $ est (≈118 calls ≈ $0.12); env-first→file key; KHÔNG log key.
- **Ghi vào runtime_memory / Builder / Translator / glossary_entries** — EVAL-ONLY; gold/oracle KHÔNG đụng.

## 3. Spec *(Claude viết)*

**`pipeline/eval/memory_tradeoff.py` (MỚI):**

- `build_override_set(report, db, *, chapter_scope) -> list[OverrideItem]`
  - Đọc per-term `forms_used` + `status` cho S0 và S1 từ report D-scorer.
  - `dominant(forms) = argmax count` (tie → surface nhỏ nhất theo lex, tất định).
  - `OverrideItem` ⟺ `status_S0 == 'consistent'` **AND** `dominant(S1) != dominant(S0)` (so theo surface đã chuẩn-hóa giống D-scorer v2_2).
  - Mỗi item: `{term, tier, s0_surface, s1_surface, s0_count, s1_count, rep_block_id, en_sentence, status_S1}`.
  - **rep occ:** block_id nhỏ nhất mà (nguồn EN chứa term qua `surface_match.find_spans(language="en")`) AND (frozen S0 target chứa `s0_surface` qua `find_spans(language="vi")`) AND (frozen S1 target chứa `s1_surface`). Nếu không có block thỏa cả 3 → đánh dấu `rep_resolved=False` (scorer bỏ qua item, ghi lý do — KHÔNG bịa).
  - Tất định; KHÔNG đọc gold/oracle.

- `build_judge_worksheet(items, frozen_targets, sources, *, seed) -> (worksheet_rows, html, key)`
  - Với mỗi item: lấy **window VI** = câu chứa surface trong frozen target tương ứng (tách câu theo `surface_match` sentence-split nếu có, else dấu câu); đánh dấu surface bằng `«...»`. `en_sentence` = câu nguồn chứa term.
  - `A,B = shuffle([s0_window, s1_window], seed_per_item)`; `key[item_id] = {A: 'S0'|'S1', B: ...}`.
  - `worksheet_row = {item_id, en_sentence, en_term, version_A, version_B, tier}` — **KHÔNG** field xuất xứ.
  - `html` = trang tĩnh: render từng item (en_sentence + en_term + Version A/B có «highlight»), radio `{A better / Tương đương / B better}` + note tùy chọn, nút **Export JSON** → `judgments_human.json` (schema `{item_id, human_label, note}`). KHÔNG nhúng key, KHÔNG nhúng S0/S1.

- `render_judge_prompt(item, orientation) -> messages`
  - Term-focused, reference-aware, mù; **ví dụ trong template = DUMMY** (`term=network` → `mạng nơ-ron` / `network`), KHÔNG dùng case 59.
  - Nội dung: câu nguồn EN + `en_term` + Version A (VI, «marked») + Version B (VI, «marked») + luật: chỉ phán term «marked», so nghĩa nguồn + tự nhiên + register, KHÔNG đoán xuất xứ, cho phép `equivalent`. Output JSON `{"label":"A_better|B_better|equivalent","confidence":0-1,"reason":"<1 câu VI>"}`.

- `resolve_pair(run1_label, run2_label) -> {system_better|'ambiguous'}`
  - run1 (A=S0,B=S1), run2 (A=S1,B=S0) → quy về hệ. **Đồng thuận** (cả 2 chỉ cùng 1 hệ tốt hơn, hoặc cả 2 'equivalent') → `S0_better|S1_better|equivalent`. **Mâu thuẫn** (đổi đáp án theo vị trí) → `ambiguous` (conservative — KHÔNG tính harm/improve).
  - `final_label`: `S1_better→improve`, `S0_better→harm`, `equivalent|ambiguous→lateral`.

**`pipeline/scripts/memory_tradeoff_build.py`:** `build_override_set` + `build_judge_worksheet` → ghi `data/eval/memory_tradeoff/{override_set.csv, worksheet.jsonl, worksheet.html, KEY/worksheet_KEY.json, KEY/README.md}`. README = "DO NOT OPEN until judgments_human.json + judgments_gemini.jsonl complete — opening reveals provenance and breaks the blind." Pin seed vào provenance. In breakdown tier.

**`pipeline/scripts/memory_tradeoff_judge.py`:** đọc `worksheet.jsonl` → cho mỗi item 2 call đảo qua judge Gemini (tái dùng client EV-02); `--preview-only` in {model, #calls=2×n, #tokens, $ est}; chạy thật chỉ sau `--confirm-token`; cache theo (item_id, orientation, model, prompt-hash) → `data/eval/memory_tradeoff/judgments_gemini.jsonl`. Guard: `judge_model` KHÔNG được == translation model (raise).

**`pipeline/scripts/memory_tradeoff_score.py`:** **safeguard** — chỉ đọc `KEY/worksheet_KEY.json` khi `judgments_gemini.jsonl` (đủ 2×n) **và** `judgments_human.json` tồn tại; else raise "blind not complete". Join key + judgments → resolve → `data/reports/memory_tradeoff_59_overrides.json`:
```
items:    [ {item_id, term, tier, consistency_status, s0_surface, s1_surface,
             gemini_run1, gemini_run2, gemini_resolved, human_label|null, final_label} ]
summary:  { n, improve_pct, lateral_pct, harm_pct, by_tier:{...} }
iaa:      { human_n, agreement_human_vs_gemini, kappa:null, single_annotator:true }
caveats:  [ "N=59 diagnostic", "solo annotator → no kappa → occurrence-level diagnostic_only", ... ]
```

**Ràng buộc (LOCK):** observe⊥compute; freeze (chỉ đọc frozen output + report); EVAL-ONLY (0 ghi runtime/Builder/glossary); blind-at-generation; judge họ-ngây-thơ ≠ translator; cost-gate Gemini; post-hoc-only (test assert KHÔNG gọi translate/builder path); dummy-example trong prompt.

## 4. Acceptance criteria *(lệnh chạy được + LOCK)*

```bash
cd THESIS_RUNTIME_TOOL

# (1) override-set + worksheet mù + prompt + resolve — offline/unit
python -m pytest pipeline/tests/test_memory_tradeoff.py -v
#   test_override_set_deterministic     : chạy 2 lần khớp; rule = status_S0=='consistent' AND dom(S1)!=dom(S0)
#   test_override_count_crosscheck      : len ≈ 59 (±) + breakdown tier; FAIL-LOUD nếu rỗng/0 (report-shape mismatch)
#   test_worksheet_is_blind             : worksheet_row + html KHÔNG có field/string 'S0'/'S1'/'glossary'/provenance
#   test_key_separate_folder            : key ở data/eval/memory_tradeoff/KEY/ + README 'DO NOT OPEN' tồn tại
#   test_prompt_uses_dummy_not_real     : render_judge_prompt template chứa dummy 'network'; KHÔNG chứa surface nào của 59 override (vd 'luật','khung phần mềm')
#   test_judge_swaps_two_orientations   : mỗi item sinh đúng 2 messages (A=S0/B=S1) & (A=S1/B=S0)
#   test_resolve_conservative           : run1/run2 mâu thuẫn (đổi theo vị trí) → 'ambiguous' (KHÔNG harm/improve)
#   test_resolve_mapping                : S1_better→improve, S0_better→harm, equivalent/ambiguous→lateral
#   test_scorer_refuses_key_before_done : thiếu judgments_human/gemini → raise 'blind not complete' (KHÔNG đọc key)
#   test_human_subset_stratified        : subset 12–15 rải tier (≥1 mỗi tier có mặt); shuffle(seed) tất định
#   test_judge_model_not_translator     : judge_model == translation model → raise
#   test_eval_only_no_runtime_write     : pipeline KHÔNG mở DB ghi runtime_memory/glossary_entries (chỉ đọc)

# (2) BUILD override-set + worksheet mù (0-API, tất định)
python -m pipeline.scripts.memory_tradeoff_build \
  --db data/jobs/d2l_p1/memory.sqlite3 \
  --report data/reports/d2l_translation_metrics_v2.json \
  --seed 42 --out-dir data/eval/memory_tradeoff
#   → override_set.csv (in #term + breakdown tier), worksheet.jsonl, worksheet.html, KEY/{worksheet_KEY.json,README.md}
#   chạy lại → override_set.csv + worksheet.jsonl byte-identical (tất định)

# (3) JUDGE Gemini — qua COST-GATE (preview trước, KHÔNG tiêu ngầm)
python -m pipeline.scripts.memory_tradeoff_judge \
  --worksheet data/eval/memory_tradeoff/worksheet.jsonl \
  --out data/eval/memory_tradeoff/judgments_gemini.jsonl --preview-only
#   preview PHẢI in: judge_model = Gemini (KHÔNG gpt-5.4/Claude/GLM), #calls = 2×n (~118), #tokens, $ est (~$0.12)
#   chạy thật chỉ sau confirm-token; cache (item_id,orientation,model,prompt-hash)

# (4) NGƯỜI chấm: mở worksheet.html → chấm subset 12–15 (mù) → export judgments_human.json
#   (thao tác tay; html tự sinh, không phải feature app)

# (5) CHẤM + BÁO CÁO (sau khi có cả 2 judgment)
python -m pipeline.scripts.memory_tradeoff_score \
  --workdir data/eval/memory_tradeoff \
  --out data/reports/memory_tradeoff_59_overrides.json
#   → improve%/lateral%/harm% + by_tier + agreement người↔Gemini + caveats; refuse nếu thiếu judgment

# (6) frozen DB bất biến + full suite
python -m pytest pipeline/tests app/backend/tests -q
```

**LOCK kỹ thuật (ghi §5/§6):**
- **L1 BLIND-AT-GENERATION (3-file tách):** worksheet + html mù; KEY ở folder riêng + README "DO NOT OPEN"; A/B hoán vị seed; scorer raise nếu đọc key trước khi 2 judgment đủ. Kiểm bằng `test_worksheet_is_blind` + `test_key_separate_folder` + `test_scorer_refuses_key_before_done`.
- **L2 DUMMY-EXAMPLE:** prompt template dùng `network` (ngoài 59); CẤM mọi surface của 59 trong template/spec → spec public không làm vỡ mù. `test_prompt_uses_dummy_not_real`.
- **L3 JUDGE NGÂY-THƠ ≠ TRANSLATOR:** Gemini (pin model+temp0+seed); raise nếu judge_model==translation model; KHÔNG Claude/GLM. `test_judge_model_not_translator`.
- **L4 ĐẢO VỊ TRÍ + RESOLVE CONSERVATIVE:** 2 call/item; mâu thuẫn→ambiguous (KHÔNG đếm harm/improve); equivalent cho phép. `test_judge_swaps_two_orientations` + `test_resolve_conservative`.
- **L5 REFERENCE-AWARE TERM-FOCUSED:** prompt có câu nguồn EN + term «marked»; phán adequacy vs nghĩa nguồn, chỉ term marked.
- **L6 NEO NGƯỜI MÙ + SOLO CAVEAT:** người chấm subset stratified 12–15 cùng worksheet mù; agreement người↔Gemini quyết tin Gemini hay không (cao→trust+report "1 human+LLM cross-check"; thấp→người chấm hết 59). **Solo → κ=null → occurrence-level `diagnostic_only`** (nối EV-05 L11). `test_human_subset_stratified`.
- **L7 0-API ≠ HARM:** `consistency_status` (form khác) là 0-API; `improve/lateral/harm` BẮT BUỘC qua judge. Report tách 2 cột, KHÔNG gọi drift là harm.
- **L8 POST-HOC + EVAL-ONLY:** chỉ đọc frozen `translation_runs` + report + nguồn EN; KHÔNG re-translate/re-run Builder/ghi runtime/đụng gold. `test_eval_only_no_runtime_write` + frozen DB hash trước==sau.
- **L9 KHÔNG TUNE-ON-TEST:** 59 override CHỈ báo cáo; CẤM dùng để prune injection S1 headline. Sửa hệ = arm `S1_better` a-priori RIÊNG (task khác).
- **L10 COST-GATE Gemini:** preview {model,#calls,#tokens,$est} → confirm-token → chạy; cache; KHÔNG log key; env-first→file.
- **L11 DIAGNOSTIC:** N=59 nhỏ; ngưỡng quyết-định **gợi ý** (harm <10% → S1 giữ headline, chỉ bank finding; 10–30% → spec `S1_better` pilot; >30% → `S1_better` là contribution chính), KHÔNG phải cổng cứng.

**Follow-up (ngoài scope, ghi nhận):**
1. Nếu harm ≥10% → task `EV_D2L_07_s1_better` = glossary 2-tầng (hard vetted / soft advisory / never symbols+số+common-word) + cổng do-no-harm canonical, **a-priori source-side** (KHÔNG lọc từ S0 test), chạy arm `S1_better` 1 chương pilot so S0/S1/S1_better — KHÔNG ghi đè frozen S1.
2. Canonical vetting 0-API (rule-based: loại số/ký hiệu `-1`/`16kHz`; cờ canonical 1-âm-tiết common-word) làm input cho (1).
3. Mở rộng judge ra toàn 4 chương / occurrence-level chỉ sau khi có annotator-2 (κ) — hiện diagnostic.

## 5. Implementation notes *(CodeX điền)*

<!-- CodeX: điền files changed / implemented / deviation / commands run / not run. KHÔNG commit. -->

## 6. Review *(Claude điền)*

<!-- Claude: tái kiểm độc lập (KHÔNG tin §5) — frozen hash, tự chạy test, blind thật (mở 3 file kiểm không lộ xuất xứ), judge≠translator, dummy-not-real, resolve conservative, EVAL-only no-write, cost-gate. -->
