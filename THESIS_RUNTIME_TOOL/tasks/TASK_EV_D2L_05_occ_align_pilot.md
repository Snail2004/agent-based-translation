# TASK_EV_D2L_05_occ_align_pilot — OCC_ALIGN_PILOT_01: occurrence-level alignment **PILOT** (1 chương) — 2 nhánh **NGANG HÀNG** (independent aligner ⟂ self-report+verify), **audit NGƯỜI quyết**, **post-hoc trên frozen output**, KHÔNG re-translate

- **Status:** DONE / PASS (impl) (Claude, 2026-06-19; reviewed §5 + code độc lập) — REVIEW (CodeX, 2026-06-19); READY (Claude, 2026-06-18)
- **Refs:** memory `d2l-scorer-validity-and-remediation-ladder`, `dont-tune-intervention-on-test-baseline` · THESIS_ARCHITECTURE_LOCK §10 (uu)/(tt)/(ss) · EV_D2L_03b (shared `pipeline/eval/surface_match.py`) · RunControl cost-gate (APP-C01) · 3-bên hội tụ Claude+GLM-5.2+user 2026-06-18.
- **Branch/Commit:** local working tree only; not committed per task protocol.

## 1. Bối cảnh & mục tiêu *(Claude viết)*

**Điểm mù còn lại sau 03b:** surface-matching (kể cả segmentation) CHỈ tìm được form đã có trong registry. Khi Translator dịch một term bằng từ NGOÀI registry → matcher MÙ. Ví dụ sống `d2l_preliminaries_index_b003`: source "a **set** of techniques" → **S0** dịch "một **bộ** kỹ thuật"; `bộ` ∉ registry `{tập hợp, tập}` → metric đếm *undetected*, KHÔNG thấy S0 thật sự dịch ra gì. Ta đang là "ông thầy không có đáp án của S0". Lỗ này surface KHÔNG BAO GIỜ vá được — nó chỉ soi được form nó đã biết để tìm.

**Cốt lõi (user nêu, đúng):** không string-match nào suy ra rendering ngoài-registry; chỉ công cụ HIỂU NGÔN NGỮ làm được. **KHUNG ổn định** = code liệt kê occurrence phía NGUỒN tiếng Anh (bên code mạnh, đã cứng hóa ở 03b `find_spans(language="en")`), công cụ ngôn ngữ điền phần chỉ-nó-biết (span đích).

**KHÔNG phải nhị phân "code ngu / hỏi chính Translator".** Có **2 ứng cử viên NGANG HÀNG**, **audit người quyết** — KHÔNG pre-rank:
- **Nhánh A — Aligner ĐỘC LẬP** (SimAlign mBERT/XLM-R, hoặc awesome-align; local, 0 paid-API, tất định khi pin model+method+seed). Không thấy glossary; **cùng một cây thước cho S0 và S1** → nhiễu đối xứng, triệt tiêu trong hiệu số S1−S0.
- **Nhánh B — Translator TỰ BÁO CÁO + code-verify**, **HẬU KỲ trên output ĐÓNG BĂNG** (KHÔNG inline — inline làm bẩn ablation; bác từ (ss)). **Model nhánh B = ĐÚNG model đã tạo `translation_runs`** (nếu không thì hết là self-report — xem §2 OUT).

**Sự thật nền = AUDIT NGƯỜI (proposer-blind), KHÔNG phải lời công cụ.** Self-report là *đề xuất alignment*, không phải ground truth. **Code-verify CHỈ bắt hallucination** (form khai không có thật trong output), **KHÔNG bắt misattribution** (trỏ vào occurrence CÓ THẬT của form-glossary nhưng thuộc từ khác — đúng lớp cross-term/sub-syllable leakage `tập`⊂`tập dữ liệu`, `AI`⊂`d2l.ai` đã tốn 4 task để diệt; `find_spans` presence-check không phân biệt *correspondence*). Vì vậy tính đúng của self-report **chỉ chốt được bằng audit người** — không có lưới chặn tự động rẻ cho lỗi nguy hiểm của nó.

**Vì sao 2 tiêu chí, không chỉ accuracy gộp:** ablation đo **hiệu số S1−S0**. Sai số *đối xứng* (aligner độc lập) triệt tiêu trong hiệu số; sai số *lệch-nhánh* (self-report: S1 biết glossary, S0 không) **KHÔNG triệt tiêu → bẻ cong chính hiệu số**, và thổi phồng đúng hướng tệ nhất. Nên gate phải đo `differential = |acc_S1 − acc_S0|`, không chỉ accuracy gộp.

**Mục tiêu pilot (1 chương `preliminaries`, ~30 block, KHÔNG re-translate):** trên gold người, đo nhánh nào phục hồi đúng span đích per-occurrence, theo (R1) accuracy RIÊNG S0 + S1 và bias-lệch-nhánh; (R3) self-report KHÔNG "gần ngang" mặc định nhờ verify — vượt cùng cổng differential như aligner. Gate quyết: occurrence-level có đủ làm **headline trục-2** không, hay chỉ **diagnostic**. **Bổ sung, KHÔNG thay** trục surface v2.2 0-API (vẫn là xương sống headline + highlight UI).

(Phụ: "consistent ≠ adequate" — ngay b003, S1 "tập hợp" theo glossary có thể KÉM tự nhiên hơn S0 "bộ" → củng cố nhu cầu trục adequacy riêng; ngoài scope task này.)

## 2. Scope

**IN (pilot, 1 chương):**
1. **KHUNG nguồn (code ổn định):** `build_occ_frame` liệt kê mọi occurrence term-registry trong block NGUỒN EN của chương qua `surface_match.find_spans(language="en")` + `allocate_spans` (longest-non-overlap, tái dùng 03b), gắn `occ_id` ổn định (`block_id` + char-offset). **Cùng frame cho 2 nhánh × 2 config ⇒ mẫu số đồng nhất.**
2. **Nhánh A (independent):** `occ_align --method simalign` — input (block nguồn EN, output ĐÍCH VI đóng băng từ `translation_runs`); pin model + align-method (argmax/itermax) + seed; map alignment token→char-span đích cho mỗi `occ_id`. KHÔNG đọc glossary. Áp Y HỆT S0 và S1.
3. **Nhánh B (self-report+verify, HẬU KỲ):** `occ_align --method selfreport` — lượt RIÊNG; đưa (block nguồn EN đánh dấu `occ_id` bằng sentinel, **output đích VI ĐÃ ĐÓNG BĂNG mà chính config đó đã tạo**); yêu cầu mỗi `occ_id` → `target_span` (trích từ CHÍNH output đó) | `NOT_RENDERED` | `FUSED`. Prompt S0 và S1 **cùng template**; **prompt S0 KHÔNG chứa glossary**. **Model = model đã dịch.** **Code-verify:** surface khai phải xuất hiện đúng offset trong frozen output (qua `find_spans(language="vi")`) else `status=instrument_error`. Cache theo (config, block, model, prompt-hash).
4. **GOLD người (proposer-blind):** `occ_align_sample` sinh template gold **stratified** (cap ≤3 occ/term, rải status×tier) cho chương pilot; annotator thấy (occ nguồn, TOÀN output đích), đánh dấu span đúng | none — **KHÔNG thấy đề xuất nhánh nào**. ≥2 annotator NGƯỜI; tính κ. (Solo → xem L1 plan-B.)
5. **Chấm + GATE:** `occ_align_audit` đọc đề xuất 2 nhánh + gold → `data/reports/occ_align_pilot.json`: `acc_S0`, `acc_S1` RIÊNG từng nhánh, `differential`, hallucination/miss-rate, κ, `single_annotator`, blindspot, `gate.decision`.
6. **Định lượng điểm mù registry:** liệt kê occ có gold-span NHƯNG form ∉ registry (vd set→bộ) — đo cái surface bỏ sót.

**OUT (CẤM):**
- **Inline self-emit lúc dịch** (bẩn ablation). Self-report CHỈ hậu kỳ trên frozen output.
- **RE-TRANSLATE / đổi prompt dịch S0/S1 / ghi DB frozen.** Dùng `translation_runs` có sẵn.
- **Bơm glossary vào prompt báo cáo của S0** (phá đối xứng / Directional-Lock).
- **Nhánh B dùng model KHÁC model dịch** → hết là self-report, thành LLM-aligner thứ ba; nếu muốn thì tách **Branch C "independent LLM-aligner"** RIÊNG, KHÔNG gộp vào B.
- **Coi self-report là ground truth / bỏ audit người.** Gold = người.
- **Dùng code-verify (find_spans presence) làm bằng chứng correspondence** — chỉ chống hallucination, KHÔNG chống misattribution.
- **Pre-rank** nhánh trước khi có gold (A và B ngang hàng tới khi đo).
- **Mở rộng > 1 chương trước khi gate L4 qua.**
- **Tiêu API ngầm** — nhánh B qua cost-gate + prompt-preview (RunControl, APP-C01) với confirm-token; in dự toán trước.
- **Gold non-stratified hoặc neo theo đề xuất** (anchoring) — phải proposer-blind + shuffle(seed) + cap ≤3/term (bài học sampling-bias EV-03b: harness cũ sort alphabet → 6 term, 100% drift).
- **Chỉnh ngưỡng τ sau khi thấy kết quả** (τ chốt a-priori ở §4).
- Tuyên bố pilot "đã có headline occurrence-level" trước khi L4 cho phép (đặc biệt khi `single_annotator=true`).

## 3. Spec *(Claude viết)*

**`pipeline/eval/occ_align.py` (MỚI):**
- `build_occ_frame(blocks, terms, *, chapter) -> list[OccItem]` — `find_spans(text, src, language="en")` + `allocate_spans` trên NGUỒN; `OccItem{occ_id, block_id, src_term, src_char_span, sentence_en}`. Tất định.
- `align_independent(occ_frame, frozen_target, *, model, method, seed) -> dict[occ_id, Proposal]` — SimAlign (token nguồn↔đích); map về char-span đích. `Proposal{occ_id, target_span|None, target_surface|None, source}`. **Cache (model_version, method, seed, block_id, config) → JSONL hash-stable; re-run byte-identical (khỏi recompute 60–180s).**
- `align_selfreport(occ_frame, frozen_target, *, config, model) -> dict[occ_id, Proposal]` — render prompt hậu kỳ (sentinel occ_id trên nguồn + **frozen output đích**); `model` = model đã tạo `translation_runs`; parse → `target_span | NOT_RENDERED | FUSED`. **S0 template KHÔNG glossary.**
- `verify_presence(frozen_target, proposal) -> Proposal` — set `present:bool`; `frozen_target[span]==claimed_surface` qua `find_spans(language="vi")`; present=False ⇒ hallucination ⇒ `instrument_error`. **DOC: verify≠correspondence (không chống misattribution).**

**`pipeline/scripts/occ_align_sample.py`:** sinh `data/eval/occ_align_gold_<chapter>.csv` (`occ_id, block_id, src_term, status, tier, sentence_EN, target_output_VI, [TRỐNG: gold_target_span, gold_surface, annotator, note]`); **stratified = `shuffle(seed=42)` trong từng stratum (tier×status) rồi slice, cap ≤3/term**; seed pin trong sampler + ghi provenance (chống lặp bias alphabet EV-03b); KHÔNG cột nào lộ đề xuất.

**`pipeline/scripts/occ_align_audit.py`:** proposals 2 nhánh + gold → `occ_align_pilot.json`:
```
per_branch: { simalign:{acc_S0,acc_S1,differential,hallucination_rate,miss_rate,n},
              selfreport:{acc_S0,acc_S1,differential,hallucination_rate,miss_rate,n} }
iaa:        { kappa, n_annotators, single_annotator }
blindspot:  { registry_missed_occ, examples:[{occ_id, gold_surface}] }   # set->bộ class
gate:       { decision, headline_branch|null, crosscheck_branch|null, rationale }
```
accuracy = (#occ proposal.span==gold.span) / (#gold occ); `None==None` (NOT_RENDERED khớp gold-none) tính đúng.

**Ràng buộc (LOCK):** observe⊥compute; freeze (chỉ đọc frozen output); 0 paid-API nhánh A; nhánh B qua cost-gate; post-hoc-only (test assert KHÔNG gọi translate path); deterministic nhánh A; Directional-Lock (S0 report mù glossary).

## 4. Acceptance criteria *(lệnh chạy được + LOCK)*

```bash
cd THESIS_RUNTIME_TOOL

# (1) KHUNG + 2 nhánh + verify + sampler — offline/unit
python -m pytest pipeline/tests/test_occ_align.py -v
#   test_occ_frame_deterministic       : frame 2 lần khớp; occ_id ổn định
#   test_selfreport_posthoc_only       : align_selfreport KHÔNG gọi translate; chỉ đọc frozen_target
#   test_selfreport_model_is_translator: model nhánh B == model đã tạo translation_runs (else raise/cảnh báo "không phải self-report")
#   test_verify_catches_hallucination  : khai surface không-có-trong-output → present=False/instrument_error
#   test_verify_misses_misattribution  : khai form-glossary CÓ THẬT nhưng thuộc occ khác → verify PASS (DOC: verify≠correspondence, cần audit)
#   test_s0_prompt_has_no_glossary     : render prompt S0 → KHÔNG chứa term glossary
#   test_single_annotator_blocks_headline : single_annotator=true + acc=0.95 + differential=0.01 → gate.decision==diagnostic_only (KHÓA L4, chống regression)
#   test_sampler_stratified_shuffle    : ≤3 occ/term; shuffle(seed=42) tất định; proposer-blind (không cột đề xuất)
#   test_not_rendered_option           : occ bị bỏ dịch → NOT_RENDERED hợp lệ, KHÔNG bịa span
#   test_simalign_cache_byte_identical : nhánh A chạy 2 lần → output byte-identical (cache hash-stable)

# (2) Nhánh A độc lập — 0 paid-API, tất định
python -m pipeline.scripts.occ_align --method simalign --config S0 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --out data/eval/occ_align_simalign_S0.jsonl
python -m pipeline.scripts.occ_align --method simalign --config S1 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --out data/eval/occ_align_simalign_S1.jsonl
#   chạy lại → byte-identical (deterministic; pin model+method+seed; cache (model,method,seed,block,config))

# (3) Nhánh B self-report — HẬU KỲ, qua COST-GATE (preview trước, KHÔNG tiêu ngầm)
python -m pipeline.scripts.occ_align --method selfreport --config S0 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --out data/eval/occ_align_selfreport_S0.jsonl --preview-only
#   preview PHẢI in: model = ĐÚNG model đã tạo translation_runs (KHÔNG thay model rẻ hơn — đổi model = đổi instrument),
#                    #calls, #tokens, $ est.
#   Ước lượng tham khảo (CHỐT theo model dịch thật): ~60 calls (30 block × 2 config), ~30k token,
#                    ~$0.05 (Gemini Flash) / ~$0.02 (GPT-4o-mini) — illustrative, không phải con số khóa.
#   chạy thật chỉ sau user confirm-token; cache lại.

# (4) GOLD người — sampler proposer-blind, stratified shuffle(seed)
python -m pipeline.scripts.occ_align_sample --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --cap-per-term 3 --seed 42 --out data/eval/occ_align_gold_d2l_preliminaries.csv
#   → cột gold_* TRỐNG để annotator NGƯỜI điền; seed ghi vào provenance

# (5) CHẤM + GATE (sau khi gold điền)
python -m pipeline.scripts.occ_align_audit \
  --proposals data/eval/occ_align_simalign_S0.jsonl data/eval/occ_align_simalign_S1.jsonl \
             data/eval/occ_align_selfreport_S0.jsonl data/eval/occ_align_selfreport_S1.jsonl \
  --gold data/eval/occ_align_gold_d2l_preliminaries.csv \
  --out data/reports/occ_align_pilot.json

# (6) frozen DB bất biến + full suite
python -m pytest pipeline/tests app/backend/tests -q
```

**LOCK kỹ thuật (ghi §5/§6):**
- **L1 GOLD = NGƯỜI, proposer-blind; PLAN-B solo:** ≥2 annotator người; κ≥0.7 CHỈ khi 2 người thật (CẤM 2-session-model = self-consistency). Annotator KHÔNG thấy đề xuất nhánh nào. **Nếu chỉ 1 annotator người (tác giả solo): set `single_annotator=true`, IAA=N/A; occurrence-level CHỈ ở mức `diagnostic` (KHÔNG headline) dù acc≥0.90 — chờ annotator-2 (đủ trên 1 SUBSAMPLE ~30 occ) mới unlock headline. Test-retest cùng người = intra-annotator (lower-bound), KHÔNG thay IAA.**
- **L2 ĐỐI XỨNG + S0 mù glossary:** cùng KHUNG occ cho 2 config/2 nhánh; prompt báo cáo S0 KHÔNG chứa glossary (Directional-Lock).
- **L3 POST-HOC + verify≠correspondence:** self-report chỉ trên frozen output (không inline, không re-translate); code-verify chống hallucination, KHÔNG chống misattribution — ghi rõ, đừng nhầm là bằng chứng đúng.
- **L4 GATE (R1+R3, KHÔNG pre-rank, τ a-priori):** một nhánh đủ tư cách HEADLINE occurrence-level ⟺ `min(acc_S0,acc_S1) ≥ 0.90` **VÀ** `differential ≤ 0.03` (trên gold người) **VÀ `single_annotator=false`**. `single_annotator=true` → `decision='diagnostic_only'` bất kể acc/differential. Cả 2 đủ → headline = nhánh có `min(acc)` cao hơn, nhánh kia cross-check (error-bar). Chỉ 1 đủ → nhánh đó headline. **Không nhánh nào đủ → occurrence-level = DIAGNOSTIC, surface v2.2 vẫn headline**, alignment báo exploratory error-bar rộng. τ chốt tại spec này, CẤM chỉnh sau khi thấy kết quả.
- **L5 0-API nhánh A (+cache); nhánh B qua COST-GATE:** SimAlign local pin (model+method+seed) + cache (model,method,seed,block,config) → re-run byte-identical; self-report qua RunControl prompt-preview + confirm-token + $ est, **model = model dịch thật**, cache; KHÔNG tiêu ngầm.
- **L6 PILOT 1 chương:** preliminaries duy nhất; CẤM scale 4 chương trước khi L4 qua; KHÔNG re-translate.
- **L7 dual-axis & bổ sung:** trục-2 occurrence-level; surface v2.2 (0-API) GIỮ làm xương sống headline + highlight UI. Thesis báo song trục khi gate cho phép.

**Follow-up (ngoài scope, ghi nhận):** alignment cho form-per-occurrence nhưng *quyết "các X ≡ X"* vẫn cần luật tương đương lúc COMPARE → morph-norm (EV_D2L_03d) có thể vẫn cần ở tầng so sánh kể cả khi alignment tốt. Sequencing: **pilot này TRƯỚC morph-norm** (kết quả pilot quyết morph-norm có cần không).

## 5. Implementation notes *(CodeX điền)*

### Files changed

- Added `pipeline/eval/occ_align.py`.
- Added scripts:
  - `pipeline/scripts/occ_align.py`
  - `pipeline/scripts/occ_align_sample.py`
  - `pipeline/scripts/occ_align_audit.py`
- Added `pipeline/tests/test_occ_align.py`.
- Added dependency pin `simalign==0.4` to `pipeline/requirements.txt`.
- Generated pilot artifacts:
  - `data/eval/occ_align_gold_d2l_preliminaries.csv` (60 proposer-blind rows; gold fields empty)
  - `data/eval/occ_align_simalign_S0.jsonl` (32 proposal rows, sample scope)
  - `data/eval/occ_align_simalign_S1.jsonl` (28 proposal rows, sample scope)

### Implemented

- `build_occ_frame(...)` reuses `surface_match.allocate_spans(..., language="en")` and stable `occ_id = block_id:start:end:term_slug`.
- Branch A `--method simalign` uses `simalign==0.4`, default `model=bert`, `method=itermax`, `seed=42`, no paid API.
- Branch A has per-block JSONL cache under `data/cache/occ_align/` and filters cached rows by current `occ_id` sample scope.
- Branch B `--method selfreport` has:
  - post-hoc prompt over frozen source/target only;
  - identical S0/S1 template;
  - no glossary payload in S0 or S1 prompt;
  - model identity guard against recorded `translation_runs.model`;
  - `--preview-only` cost/token estimate and confirm-token;
  - real run path protected by `--confirm-token` and `LLMClient` cache.
- `occ_align_sample` now has `--max-rows` (default 60) in addition to `--cap-per-term`; without this the chapter would emit 1644 rows, which is not a human-audit pilot.
- `occ_align_audit` supports single-annotator diagnostic gate, per-branch S0/S1 accuracy, differential, hallucination/miss rate, simple Cohen kappa when two real annotators are present, and registry blindspot count (`gold_surface` outside accepted forms).

### Important deviation / finding

The original task estimate for `d2l_preliminaries` was too small. Real frame from current DB:

- source occurrences in chapter: **2041**
- full self-report preview without sample: **292 calls**, **246,268 prompt tokens**, **1,196,032 max output tokens**, estimated worst-case **$2.453631** for S0 alone
- full SimAlign S0 command timed out at 10 minutes and later produced a full 2041-row output, showing that full-chapter CPU alignment is not a small pilot operation.

Therefore implementation keeps full mode available but the practical pilot path is sample-scoped:

```bash
python -m pipeline.scripts.occ_align_sample --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --cap-per-term 3 --max-rows 60 --seed 42 \
  --out data/eval/occ_align_gold_d2l_preliminaries.csv

python -m pipeline.scripts.occ_align --method simalign --config S0 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --sample-csv data/eval/occ_align_gold_d2l_preliminaries.csv \
  --out data/eval/occ_align_simalign_S0.jsonl

python -m pipeline.scripts.occ_align --method simalign --config S1 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --sample-csv data/eval/occ_align_gold_d2l_preliminaries.csv \
  --out data/eval/occ_align_simalign_S1.jsonl
```

This is not a silent relaxation: it matches the human-audit pilot size (60 rows) and avoids spending either API money or excessive CPU on un-audited occurrences.

### Commands run

```bash
python -m pip install simalign==0.4

python -m pytest -p no:cacheprovider pipeline\tests\test_occ_align.py -q
# 12 passed in 1.78s

python -m pipeline.scripts.occ_align_sample --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --cap-per-term 3 --max-rows 60 --seed 42 \
  --out data/eval/occ_align_gold_d2l_preliminaries.csv
# occurrences=2041, rows=60

python -m pipeline.scripts.occ_align --method selfreport --config S0 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --sample-csv data/eval/occ_align_gold_d2l_preliminaries.csv \
  --out data/eval/occ_align_selfreport_S0.jsonl --preview-only
# calls=31, occurrences=32, estimated_prompt_tokens=18240,
# estimated_max_output_tokens=126976, estimated_uncached_cost_usd=0.258512,
# confirm_token=719da84846ad

python -m pipeline.scripts.occ_align --method selfreport --config S1 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --sample-csv data/eval/occ_align_gold_d2l_preliminaries.csv \
  --out data/eval/occ_align_selfreport_S1.jsonl --preview-only
# calls=26, occurrences=28, estimated_prompt_tokens=16788,
# estimated_max_output_tokens=106496, estimated_uncached_cost_usd=0.217189,
# confirm_token=99bdbb9acacc

python -m pipeline.scripts.occ_align --method simalign --config S0 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --sample-csv data/eval/occ_align_gold_d2l_preliminaries.csv \
  --out data/eval/occ_align_simalign_S0.jsonl
# rows=32, cache=per_block

python -m pipeline.scripts.occ_align --method simalign --config S1 --chapter d2l_preliminaries \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --sample-csv data/eval/occ_align_gold_d2l_preliminaries.csv \
  --out data/eval/occ_align_simalign_S1.jsonl
# rows=28, cache=per_block

python -m pytest -p no:cacheprovider pipeline\tests\test_occ_align.py pipeline\tests\test_surface_match.py pipeline\tests\test_d2l_translate_score.py -q
# 31 passed in 9.92s

python -m pytest -p no:cacheprovider pipeline\tests app\backend\tests -q
# 284 passed in 162.50s
```

Pytest on Windows still prints the known atexit `PermissionError` while cleaning `D:\temp\pytest-of-Snail\pytest-current`, but exit code was 0 and all tests passed.

### Not run

- Self-report real API calls were **not** run. Only preview/cost-gate was exercised.
- `occ_align_audit` production was **not** run because `occ_align_gold_d2l_preliminaries.csv` is intentionally blank for human annotation. Unit tests cover gate behavior.

## 6. Review *(Claude điền)*

**Verdict: PASS (implementation/machinery).** Tái kiểm độc lập trên repo thật (KHÔNG tin §5).

**A. Frozen + tests (tự chạy):**
- **Frozen DB UNCHANGED:** SHA256 hiện == `DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8` (hash frozen EV-03b) → pilot chỉ ĐỌC, không ghi DB.
- Tự chạy `pytest pipeline/tests/test_occ_align.py` → **12 passed**. WinError temp-symlink lúc thoát = noise đã biết, benign.

**B. LOCK enforce + CÓ test (không chỉ tài liệu):**
- L3 verify≠correspondence: `test_verify_catches_hallucination` + `test_verify_misses_misattribution` (form-glossary có thật nhưng sai occ → verify PASS, ghi rõ cần audit). ✅
- L2 S0 mù glossary: `test_s0_prompt_has_no_glossary`. ✅
- Branch B model=translator: script `raise SystemExit` nếu `llm_config.model != translation_model`; `test_selfreport_model_is_translator` đọc `gpt-5.4-mini`. ✅
- L4 gate a-priori (hardcode `min(acc)>=0.90 AND differential<=0.03`, KHÔNG chỉnh-sau) + single_annotator→`diagnostic_only`: `test_single_annotator_blocks_headline` (acc=1.0 vẫn diagnostic). ✅
- L1/R2 sampler stratified shuffle(seed=42)+cap/term+proposer-blind+provenance: `test_sampler_stratified_shuffle`. ✅
- L5 cost-gate (preview→confirm_token) + cache byte-identical: 2 test. ✅

**C. Integration THẬT (chống green-tests-che-dead-integration):**
- Production wire `make_simalign_aligner` (SimAlign thật) vào ĐÚNG `align_independent` mà test wire FakeAligner → path production == path test.
- Artifacts THẬT: S0 32 / S1 28 rows, nội dung hợp lý (gradients→"gradient", variable→"biến", bert/itermax). 3 S0 + 1 S1 no-align = `_missing_proposal` (SimAlign không căn được → human gold phân xử). Aligner rows == gold rows từng config. ✅
- **Ngữ cảnh GIỮ khi sample-scoped (rủi-ro-validity #1):** `align_independent`/`render_selfreport_messages` đọc NGUYÊN block source+target; `--sample-csv` chỉ lọc occ EMIT, KHÔNG cắt input align → đã loại.

**D. Sample-scoping deviation: CHẤP NHẬN + mình NHẬN lỗi spec.** Full self-report = 2041 occ → 292 calls / 246k tok / ~$2.45 (S0 riêng). **Ước lượng ~$0.05 trong spec của mình SAI** (tưởng ~60 calls tổng; thực per-block full-chapter). `--max-rows 60` + `--sample-csv` rẻ HƠN và đúng mục đích pilot hơn. Stratification đa dạng: tier {preserve16/hard17/soft14/ignore8/entity5}, status {drift16/consistent17/undetected10/unknown17} — KHÔNG lặp bias EV-03b (6 term/100% drift).

**E. Follow-up (KHÔNG chặn PASS):**
1. **Self-report localize = `find_spans[0]` (match đầu)** → rủi ro misattribution khi target-surface lặp. **Mình đo thực tế: 0/60 occ at-risk** (không occ nào có cùng src_term ≥2× trong 1 block+config) → KHÔNG ảnh hưởng pilot này, chạy as-is OK. Sửa (offset/Nth-match/context-anchor) chỉ khi scale ra sample có surface lặp.
2. SimAlign cold-start determinism chưa tự re-run (dựa pin simalign==0.4 + cpu + seed + per-block-cache + test cache).
3. 17/60 status "unknown" = registry-term trong source nhưng không ở `terms_all` đã chấm (occ<ngưỡng) → bình thường; gold người vẫn chấm.

**F. Pilot CHƯA chạy thật — 3 bước USER trước gate decision:**
1. Self-report thật: `occ_align --method selfreport --sample-csv … --preview-only` → confirm-token → chạy thật (gpt-5.4-mini; preview thực S0 31 calls/$0.26, S1 26 calls/$0.22 — tổng ~$0.48, KHÔNG phải $2.45).
2. **Hand-label 60 dòng gold** (cột `gold_*`). **Solo → gate = `diagnostic_only`** dù acc cao; muốn headline cần annotator-2 NGƯỜI trên (ít nhất) subsample để có κ.
3. `occ_align_audit` → `occ_align_pilot.json` (acc_S0/S1, differential, κ, blindspot, gate).

**Headline:** chưa có — đúng thiết kế (gate chờ gold người). Machinery PASS, sẵn sàng chạy.
