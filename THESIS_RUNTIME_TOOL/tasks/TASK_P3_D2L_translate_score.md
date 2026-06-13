# TASK_P3_D2L_translate_score — Dịch S0/S1 trên 4 chương D2L + chấm headline B (TAR-vs-gold) + D (consistency) + A/C chẩn đoán + judge sample

- **Status:** READY
- **Refs:** LOCK (hh) (injection policy occ≥2 dataset-aware + `injection_role` derive trong
  code), (dd) (4 thước A/B/C/D; **dual headline B+D**; allowed_variants curate eval-side;
  B occurrence-weighted; conflict≠lỗi), (ee) (dev/test split; 2 lớp claim comparative+usability),
  (gg) (kỷ luật token: preflight + per-call ceiling + UTC; đo bằng token không phải $),
  (cc) (gold EVAL-ONLY, CẤM bơm), (z-ter) (không trộn 3 thước); engine dịch sẵn = P3-01/P4-02
  (`pipeline/translate/{windower,prompt,runner}.py` + `pipeline/retrieval/context_builder.py`);
  TAR scorer sẵn = EV-01/`pipeline/eval/thesis_scoring.py` (ruler-agnostic); judge = EV-02
  (`pipeline/eval/judge.py` Gemini cross-provider); registry frozen = P2-D2L
  (`data/jobs/d2l_p1/memory.sqlite3`, glossary 1608, gold 458)
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

P2-D2L đã có registry frozen (recall 0.742, agent captured). Giờ DỊCH và RA SỐ HEADLINE cho
track kỹ thuật — nơi giá trị mạnh nhất: *S0 (không memory) trôi thuật ngữ → S1 (bơm registry)
nhất quán + đúng gold người*. Đây là thứ TI không làm được (TI không có gold → TAR chỉ đo
"vâng lời"; D2L có gold người-chuẩn → **B đo CHẤT LƯỢNG TỪ VỰNG thật, KHÔNG bão hòa 1.0**).
Đánh trúng mối lo GVHD ("đầu tác tử sau tác nhân").

## 2. Scope

**Chương:** S0/S1 dịch 4 chương benchmark đã có registry frozen: `introduction, preliminaries,
linear_networks, multilayer_perceptrons`.

**IN:**
1. **Injection adapter D2L** (mở rộng `context_builder` theo LOCK hh, KHÔNG phá đường TI):
   - Bơm term vào S1 nếu: `term có trong window` **AND** `occurrences_count ≥ 2` **AND**
     `injection_role == 'translate'`.
   - `injection_role` derive trong CODE (chưa migrate schema): `preserve` nếu
     `do_not_translate=true` hoặc `term_type ∈ {code_api, proper_noun}` hoặc khớp regex
     unit/symbol (`16kHz`, `.shape`); `translate` còn lại. KHÔNG bơm `preserve`/`reference`.
   - **Bơm canonical-only** (KHÔNG bơm allowed_variants — variants Builder còn nhiễu, vd
     `agent` có `đặc vụ`). Anchor-based (đã có), không dump registry.
   - Ghi stats mỗi run: `raw_registry / translation_eligible / preserve_count /
     hapax_dropped / injected_per_window (min/avg/max)`.
2. **Curate gold allowed_variants (eval-side, tracked)** `data/eval/d2l_gold_variants.csv`:
   - Seed từ **31 conflict P2-D2L** (Claude đã soi: TẤT CẢ là biến thể hợp lệ) → đánh dấu
     builder_target là allowed_variant của gold; cộng term nhạy cảm (agent/model/loss/...).
   - Loader; B dùng `gold_target ∪ allowed_variants` làm ruler. **File này EVAL-ONLY, KHÔNG
     bơm vào Translator** (test guard như P1-02).
3. **Dịch S0 + S1** trên 4 chương (reuse windower/prompt/runner TI; cùng model/temp/seed/
   prompt-skeleton = CEILING cố định cho cả S0/S1 — LOCK ee). S0 = no memory; S1 = inject
   (mục 1). Persist `translation_runs` (config='S0'/'S1', window_id). **Token discipline (gg):
   preflight estimator in trước + chặn nếu vượt; per-call ceiling; UTC guard** (đã có ở
   llm_client sau P2-D2L). KHÔNG resume nhầm; cache riêng nếu cần.
4. **Chấm thước** `pipeline/eval/` (reuse TAR scorer, ruler khác nhau):
   - **B = TAR vs gold-accepted (HEADLINE):** trên mỗi (block, gold_term có EN trong block),
     VI output chứa `gold_target` ∪ `allowed_variants`? **Occurrence-weighted** (mỗi cặp
     block-term = 1 đơn vị). S0 vs S1. Report kèm **flat** (mọi gold) + **recurring** (gold occ≥2).
   - **D = Term consistency (HEADLINE, gold-free):** mỗi registry term occ≥2, gom các cách
     dịch VI xuất hiện trong output → đếm distinct rendering; **đảo giữa 2 biến thể hợp lệ
     VẪN tính trôi** (B⊥D, dd). Score = tỉ lệ term dịch nhất-quán-1-dạng; S0 vs S1.
   - **A = TAR vs registry (CHẨN ĐOÁN):** S1 có nghe lời registry không (kỳ vọng cao/bão hòa).
   - (C đã có ở P2-D2L; ECS bỏ qua — D2L ít entity.)
5. **Report** `data/reports/d2l_translation_metrics.json` (tracked): B (S0/S1, flat+recurring) +
   D (S0/S1) + A (S1) + injection stats + cost/token thật + mẫu đắt giá (vd block có "agent"/
   "neural network" S0-trôi vs S1-nhất-quán cho slide demo).
6. **Judge sample (PHỤ, gated `--judge --sample N`):** S0-vs-S1 pairwise ~20–30 block via EV-02
   Gemini (cross-provider), `calibrated=false`. MẶC ĐỊNH TẮT (tốn $; bật khi user sẵn key/budget).
7. Tests offline `pipeline/tests/test_d2l_translate_score.py`: injection lọc đúng (occ≥2 +
   role, canonical-only, preserve bị loại); **guard gold-variants KHÔNG vào injection path**;
   B occurrence-weighted đúng trên fixture; D bắt trôi (S0 2 dạng → thấp, S1 1 dạng → cao);
   A tính đúng; preflight/per-call-ceiling kích hoạt trên fixture.

**OUT:** COMET/BLEU (cần bản người aivivn → EV-03); S2/S3 (P4+); full judge (chỉ sample);
migrate schema injection_role (derive code); curate toàn bộ 458 gold variants (seed conflict+
nhạy cảm, mở rộng sau); KHÔNG đụng artifact TI, `app/`, registry frozen (chỉ đọc).

## 3. Spec — chốt chi tiết

- **Ceiling cố định cho cả S0/S1** (model/temp/seed/prompt dịch): chỉ khác nhau ở memory
  injection = biến độc lập. CẤM tune prompt thiên vị S1 (ee). S0 phải là baseline no-memory
  TRUNG THỰC (không nhét thuật ngữ).
- **B KHÔNG bão hòa** (khác TI): ruler = gold người độc lập với cái bơm → S1 chỉ cao nếu
  Builder học đúng + Translator nghe. Kỳ vọng S0 ~0.4–0.6, S1 cao hơn rõ nhưng <1.0.
- **D đo trôi nội-văn-bản**, độc lập gold: kỳ vọng S0 trôi (đặc biệt thuật ngữ tái xuất nhiều
  chương), S1 khóa. Đây là bằng chứng "văn bản DÀI" trực diện nhất.
- **2 lớp claim (ee):** comparative = gap S1−S0 trên B/D; usability = bản dịch đủ tốt (đo bằng
  judge sample + user Likert, KHÔNG bằng gold) — ghi rõ usability là PHỤ, chưa SOTA.
- **Token (gg):** in preflight `windows × token_TB` trước; per-call ceiling abort; sau run copy
  cache+report làm artifact audit; nếu prompt/call phình bất thường → DỪNG truy.
- Determinism: cache → chạy lại 0 token.

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_d2l_translate_score.py -v
# PHẢI PASS (offline, fake transport): injection occ≥2+role+canonical-only; guard gold-variants
# không-injectable; B occurrence-weighted; D bắt trôi; A; preflight/ceiling kích hoạt

# Dịch S0+S1 (token discipline; in preflight trước):
python -m pipeline.scripts.run_translate --db data/jobs/d2l_p1/memory.sqlite3 --chapters introduction preliminaries linear_networks multilayer_perceptrons --configs S0 S1
# - preflight in ước lượng token + chặn nếu vượt; persist translation_runs S0/S1; in injection stats

# Chấm B/D/A + curate variants:
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --chapters ... --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_translation_metrics.json
# - in B (S0 vs S1, flat+recurring), D (S0 vs S1), A (S1); injection stats; mẫu agent/neural network

# (tùy chọn, cần GEMINI key) judge sample:
python -m pipeline.scripts.run_judge --db ... --compare S0:S1 --chapters ... --sample 30 --out data/reports/d2l_judge_pilot.json   # calibrated=false

python -m pytest pipeline/tests/ -v   # toàn bộ vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

—

## 6. Review *(Claude điền)*

—
