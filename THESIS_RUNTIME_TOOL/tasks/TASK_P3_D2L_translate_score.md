# TASK_P3_D2L_translate_score — Dịch S0/S1 trên 4 chương D2L (profile `technical_d2l_v1`) + chấm headline B (TAR-vs-gold) + D (consistency) + A chẩn đoán + judge sample

- **Status:** DONE — PASS (Claude review 2026-06-14)
- **Refs:** LOCK **(jj)** (staged pilot gate + caption→passthrough a-priori + sai-phân-loại-không-hỏng-B/D),
  **(ii)** (document_profile KHÔNG fork + base prompt D2L = ceiling per-dataset + scope chấm = scope dịch),
  (hh) (injection occ≥2 dataset-aware + `injection_role` derive code),
  (dd) (4 thước A/B/C/D; **dual headline B+D**; allowed_variants eval-side; B occurrence-weighted;
  conflict≠lỗi), (ee) (dev/test split; 2 lớp claim; **base-prompt = ceiling, injection = gap**;
  prompt KHÔNG monolithic), (gg) (token: preflight + per-call ceiling + UTC; đo bằng token),
  (cc) (gold EVAL-ONLY, CẤM bơm), (z-ter) (không trộn thước); engine dịch sẵn = P3-01/P4-02
  (`pipeline/translate/{windower,prompt,runner}.py` + `pipeline/retrieval/context_builder.py`);
  TAR scorer = EV-01/`pipeline/eval/thesis_scoring.py`; judge = EV-02 (`pipeline/eval/judge.py`);
  registry frozen = P2-D2L (`data/jobs/d2l_p1/memory.sqlite3`, glossary 1608, gold 458);
  classify block = `pipeline/ingest/d2l_markdown_loader.py:197` (rule-based first-line, deterministic)
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

P2-D2L đã có registry frozen (recall 0.742, agent captured). Giờ DỊCH và RA SỐ HEADLINE cho
track kỹ thuật — nơi giá trị mạnh nhất: *S0 (không memory) trôi thuật ngữ → S1 (bơm registry)
nhất quán + đúng gold người*. Đây là thứ TI không làm được (TI không có gold → TAR chỉ đo
"vâng lời"; D2L có gold người-chuẩn → **B đo CHẤT LƯỢNG TỪ VỰNG thật, KHÔNG bão hòa 1.0**).
Đánh trúng mối lo GVHD ("đầu tác tử sau tác nhân").

**Tiền đề thiết kế (LOCK ii):** D2L là tài liệu kỹ thuật (không thoại, không ẩn dụ) → KHÔNG
dùng được base prompt VĂN HỌC của TI ("literary translator / Newmark V / dialogue / storytelling
/ ship names"). Nhưng **KHÔNG fork pipeline**: chỉ thêm một `document_profile` (config khai báo)
gói {base prompt, block filter, injection policy}. Engine (windowing/cache/runner/scoring/logging)
DÙNG CHUNG — đây CHÍNH là luận điểm "một kiến trúc tổng quát qua nhiều thể loại".

## 2. Scope

**Chương:** S0/S1 dịch 4 chương benchmark đã có registry frozen: `introduction, preliminaries,
linear_networks, multilayer_perceptrons`. **Chạy STAGED** (xem §4): pilot `preliminaries` trước → full.

**IN:**
1. **`document_profile = technical_d2l_v1`** — config KHAI BÁO (YAML/dict) gói {`base_prompt`,
   `block_filter`, `injection_policy`}, được CÙNG MỘT code path đọc. **Guardrail (LOCK ii):**
   profile là DATA, KHÔNG phải nhánh code — CẤM `if profile=="technical": … else …` rải trong
   logic dịch/chấm (= fork trá hình). TI giữ nguyên = profile `literary_v1` (hiện trạng, không đụng).
2. **Base prompt kỹ thuật `s0_d2l_v1` / `s1_d2l_v1`** (= CEILING, LOCK ee/ii):
   - Register technical/expository, ưu tiên chính xác + nhất quán thuật ngữ. **BỎ:** "literary
     translator", Newmark V, "DIALOGUE: COMMUNICATIVE", storytelling/narrative voice, "carry over
     ship names/exclamations". **GIỮ:** JSON contract keyed by block_id, luật block_id, no-add/
     no-drop, cấm footnote/comment.
   - **Carry-over kỹ thuật:** giữ nguyên inline code/identifier, ký hiệu toán, equation/citation
     refs, units (`.shape`, `16kHz`).
   - **S0 và S1 DÙNG CHUNG base prompt D2L; khác nhau DUY NHẤT = S1 có khối injection** → gap
     S1−S0 sạch. Đóng băng giống hệt. TI giữ `s0_v1`/`s1_v1` để tái lập.
   - `purity_check()` (đang assert S0 không chứa memory) phải chạy cho **profile D2L** →
     `s0_d2l_v1` sạch gold/term.
3. **Block filter (theo profile)** — chỉ gửi Translator `heading + prose`; `code / math_block /
   image / label` **PASSTHROUGH nguyên văn** (giữ trong tài liệu, KHÔNG dịch, KHÔNG vào window).
   Lý do heading+prose (không chỉ prose): heading là phần bản dịch sách + chứa nhiều thuật ngữ;
   chênh chi phí chấp nhận được (mô phỏng: prose-only ~722k/780k → +heading ~877k/939k upper
   bound, vẫn dưới trần khi có preflight+cap). Windower/loader lọc theo profile.
   - **Quyết A-PRIORI (Stage 0, chống tuning-on-test):** caption trong `image`/`label` (alt-text,
     `:numref: … shows …`) = **PASSTHROUGH cho P3**; scope đo = **THÂN BÀI (heading+prose)**. Dịch
     caption là việc *completeness* → đẩy track usability sau; **CẤM đổi quyết định này dựa trên số
     B/D benchmark.**
4. **Injection adapter D2L** (mở rộng `context_builder` theo LOCK hh, KHÔNG phá đường TI): bơm
   term vào S1 nếu `term có trong window` AND `occurrences_count ≥ 2` AND
   `injection_role == "translate"`. `injection_role` derive trong CODE (chưa migrate schema):
   `preserve` nếu `do_not_translate=true` / `term_type ∈ {code_api, proper_noun}` / regex
   unit-symbol (`16kHz`, `.shape`); `translate` còn lại. **Canonical-only** (KHÔNG bơm
   allowed_variants — variants Builder còn nhiễu). Anchor-based, không dump registry. Stats mỗi
   run: `raw_registry / translation_eligible / preserve_count / hapax_dropped /
   injected_per_window (min/avg/max)`.
5. **Curate gold allowed_variants (eval-side, tracked)** `data/eval/d2l_gold_variants.csv`: seed
   **31 conflict P2-D2L** (Claude đã soi: TẤT CẢ là biến thể hợp lệ) + term nhạy cảm
   (agent/model/loss/…); loader; B dùng `gold_target ∪ allowed_variants`. **File EVAL-ONLY,
   KHÔNG bơm Translator** (test guard như P1-02).
6. **Dịch S0 + S1** trên 4 chương (reuse windower/runner; profile `technical_d2l_v1` cấp base
   prompt + filter + injection). S0 = no memory; S1 = inject (mục 4). Persist `translation_runs`
   (config S0/S1, window_id). **Token discipline (gg):** `--preflight-only` in ước lượng `windows
   × token_TB` + chặn nếu vượt TRƯỚC khi gọi API; per-call ceiling; UTC guard (đã có ở
   llm_client). Set `prompt_token_cap` trong `llm_translate.yaml`. Cache riêng nếu cần (không
   resume nhầm TI).
7. **Chấm thước** `pipeline/eval/` (reuse TAR scorer, ruler khác nhau):
   - **SCOPE (VALIDITY, LOCK ii): corpus chấm = ĐÚNG tập block đã gửi Translator (heading+prose).**
     B và D loại `code/math/image/label` khỏi **CẢ tử lẫn mẫu** — nếu không, gold-term nằm trong
     block passthrough sẽ kéo mẫu lên → bản dịch hoàn hảo cũng <1.0 (trần giả; phạt Translator vì
     không dịch đoạn ta bảo passthrough).
   - **B = TAR vs gold-accepted (HEADLINE):** mỗi (block∈scope, gold_term có EN trong block), VI
     output chứa `gold_target` ∪ `allowed_variants`? **Occurrence-weighted** (mỗi cặp block-term =
     1 đơn vị). S0 vs S1. Report **flat** (mọi gold) + **recurring** (gold occ≥2).
   - **D = Term consistency (HEADLINE, gold-free):** mỗi registry term occ≥2, gom rendering VI
     xuất hiện trong output(∈scope) → đếm distinct; **đảo giữa 2 biến thể hợp lệ VẪN tính trôi**
     (B⊥D, dd). Score = tỉ lệ term dịch nhất-quán-1-dạng; S0 vs S1.
   - **A = TAR vs registry (CHẨN ĐOÁN):** S1 có nghe lời registry không (kỳ vọng cao/bão hòa).
   - (C đã có ở P2-D2L; ECS bỏ qua — D2L ít entity.)
8. **Report** `data/reports/d2l_translation_metrics.json` (tracked): B (S0/S1, flat+recurring) +
   D (S0/S1) + A (S1) + injection stats + **scope** (đếm block-type translated vs passthrough) +
   cost/token thật + mẫu đắt giá (block "agent"/"neural network" S0-trôi vs S1-nhất-quán cho demo).
9. **Judge sample (PHỤ, gated `--judge --sample N`):** S0-vs-S1 pairwise ~20–30 block via EV-02
   Gemini (cross-provider), `calibrated=false`. MẶC ĐỊNH TẮT (tốn $; bật khi user sẵn key/budget).
10. **Tests offline** `pipeline/tests/test_d2l_translate_score.py`: **block filter (code/math/
    image/label KHÔNG vào window)**; injection occ≥2+role+canonical-only, preserve bị loại;
    **`purity_check` profile D2L (S0 sạch gold/term)**; **guard gold-variants KHÔNG vào injection
    path**; **scope chấm = scope dịch** (gold-term trong code-block KHÔNG vào mẫu B); B
    occurrence-weighted đúng trên fixture; D bắt trôi (S0 2 dạng → thấp, S1 1 dạng → cao); A đúng;
    preflight/per-call-ceiling kích hoạt trên fixture.

**OUT:** COMET/BLEU (cần bản người aivivn → EV-03); S2/S3 neighbor/Chroma (P4+); full judge (chỉ
sample); migrate schema injection_role (derive code); curate toàn bộ 458 gold variants (seed
conflict + nhạy cảm, mở rộng sau); hệ "đa thể loại" lớn (CHỈ 2 profile cho 2 dataset khóa luận);
dịch caption image/label (completeness → usability track); KHÔNG đụng artifact TI / `app/` /
registry frozen (chỉ đọc).

## 3. Spec — chốt chi tiết

- **document_profile (ii):** engine DÙNG CHUNG; chỉ {base_prompt, block_filter, injection_policy}
  đổi theo dataset. Profile = data, một code path. Lợi luận văn: profile là "TẤT CẢ những gì đổi
  giữa 2 thể loại" → bằng chứng tổng quát sạch cho hội đồng.
- **Base prompt D2L = CEILING (ee/ii):** đặt ĐÚNG NGAY S0, KHÔNG "prompt hoàn chỉnh dần ở S3/S4".
  S1/S2/S3 = THÊM TẦNG memory đo được, không phải prompt to dần. S0/S1 cùng base, khác duy nhất
  injection. CẤM tune base prompt thiên vị S1.
- **B KHÔNG bão hòa** (khác TI): ruler = gold người độc lập với cái bơm → S1 chỉ cao nếu Builder
  học đúng + Translator nghe. Kỳ vọng S0 ~0.4–0.6, S1 cao hơn rõ nhưng <1.0.
- **D đo trôi nội-văn-bản**, độc lập gold: kỳ vọng S0 trôi (đặc biệt thuật ngữ tái xuất nhiều
  chương), S1 khóa. Bằng chứng "văn bản DÀI" trực diện nhất.
- **Scope chấm = scope dịch (validity):** mẫu B/D chỉ trên heading+prose đã dịch.
- **Sai phân loại block KHÔNG hỏng B/D (validity, LOCK jj):** bộ lọc dịch + scorer cùng đọc CÙNG
  cột `block_type` → `scope_dịch ≡ scope_chấm` theo CẤU TRÚC; `classify_block` (rule-based
  first-line) bin sai CHỈ ăn vào *completeness* (caption sót) + nhiễu nhỏ, **KHÔNG sinh trần giả**.
  → headline dung sai cho khâu gán nhãn; caption là vấn đề usability layer-2, không phải lỗi số.
- **Chạy STAGED (LOCK jj — chống token-blowup + chống tuning-on-test):** Stage 0 đóng băng
  instrument (caption→passthrough a-priori) → Stage 1 offline tests + preflight 4 chương (0 API)
  → Stage 2 VALIDITY-PILOT `preliminaries` (**EXIT = check CƠ HỌC, KHÔNG phải B/D magnitude**) →
  Stage 3 full once. CẤM đổi profile/scorer dựa trên số benchmark. Pilot `preliminaries`
  (instrument frozen) = bản benchmark THẬT của chương đó → Stage 3 tái dùng cache (0 token).
- **2 lớp claim (ee):** comparative = gap S1−S0 trên B/D (bền với trần); usability = bản dịch đủ
  tốt (judge sample + user Likert, KHÔNG bằng gold) — ghi rõ usability PHỤ, chưa SOTA.
- **Token (gg):** prompt-optimize KHÔNG phải đòn tiết kiệm token chính; đòn chính = (a) không dịch
  code/math/image, (b) không bơm preserve/hapax, (c) không dump registry, (d) preflight. In
  preflight trước; per-call ceiling abort; phình bất thường → DỪNG truy.
- Determinism: cache → chạy lại 0 token.

## 4. Acceptance criteria — STAGED (KHÔNG chạy full ngay)

**Stage 0 — Freeze instrument (trước MỌI API call):** chốt profile `technical_d2l_v1`; caption
`image`/`label` → PASSTHROUGH (a-priori); scope đo = heading+prose. Đóng băng — cấm đổi theo số B/D.

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

# Stage 1 — offline tests + preflight CẢ 4 chương (0 API call):
python -m pytest pipeline/tests/test_d2l_translate_score.py -v
python -m pipeline.scripts.run_translate --db data/jobs/d2l_p1/memory.sqlite3 \
  --profile technical_d2l_v1 \
  --chapters introduction preliminaries linear_networks multilayer_perceptrons \
  --configs S0 S1 --preflight-only
# preflight in windows × token_TB + block-type counts; PHẢI chặn nếu vượt; 0 token thật

# Stage 2 — VALIDITY PILOT 1 chương `preliminaries` (nhiều code nhất = stress-test; instrument đóng băng):
python -m pipeline.scripts.run_translate --db data/jobs/d2l_p1/memory.sqlite3 \
  --profile technical_d2l_v1 --chapters preliminaries --configs S0 S1
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --chapters preliminaries \
  --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_pilot_preliminaries.json
# EXIT GATE (CƠ HỌC — KHÔNG phải B/D magnitude) — PHẢI đủ cả:
#  (a) 0 block code/math/image/label lọt vào window Translator
#  (b) scorer loại passthrough khỏi mẫu B/D (scope chấm = scope dịch)
#  (c) preflight/per-call ceiling kích hoạt; prompt/call min/avg/max không phình
#  (d) injection occ>=2 + role + canonical đúng; preserve (16kHz/.shape) bị loại
#  (e) audit tay 5 mẫu passthrough — đúng là non-translatable, KHÔNG phải prose bin nhầm
# (B/D/A CÓ in ra để XEM TRƯỚC, nhưng KHÔNG dùng làm điều kiện pass.)

# Stage 3 — CHỈ khi Stage 2 sạch → full 4 chương MỘT lần (instrument đóng băng; preliminaries tái dùng cache = 0 token):
python -m pipeline.scripts.run_translate --db data/jobs/d2l_p1/memory.sqlite3 \
  --profile technical_d2l_v1 \
  --chapters introduction preliminaries linear_networks multilayer_perceptrons --configs S0 S1
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 \
  --chapters introduction preliminaries linear_networks multilayer_perceptrons \
  --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_translation_metrics.json
# - in B (S0 vs S1, flat+recurring), D (S0 vs S1), A (S1); injection+scope stats; mẫu agent/NN
# (có thể chạy từng chương cho an toàn token — KHÔNG đổi profile/scorer giữa chừng)

# (tùy chọn, cần GEMINI key) judge sample:
python -m pipeline.scripts.run_judge --db ... --compare S0:S1 --chapters ... --sample 30 \
  --out data/reports/d2l_judge_pilot.json   # calibrated=false

python -m pytest pipeline/tests/ -v   # toàn bộ vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

Implemented as staged P3-D2L, no commit/push.

### Files changed

- `pipeline/translate/profiles.py`: added declarative `literary_v1` and `technical_d2l_v1` profiles.
- `pipeline/translate/prompt.py`: prompt construction now reads profile data; D2L S0 stays memory/gold-free.
- `pipeline/translate/windower.py`: added optional block-type filter.
- `pipeline/retrieval/context_builder.py`: profile-aware injection filter; D2L injects only canonical translate terms with `occurrences_count >= 2`.
- `pipeline/scripts/run_translate.py`: added `--profile`, `--configs`, `--preflight-only`; key loading prefers `OPENAI-KEY-2.txt` and never logs key values.
- `pipeline/eval/d2l_translate_score.py`: added D2L B/TAR-vs-gold, D/registry-consistency, A/TAR-vs-registry scorer.
- `pipeline/scripts/score_run.py`: added D2L scoring path via `--chapters`; legacy TI scoring preserved.
- `data/eval/d2l_gold_variants.csv`: eval-only variants seeded from the 31 accepted P2-D2L conflicts.
- `pipeline/configs/llm_translate.yaml`: added `prompt_token_cap: 6000`.
- `pipeline/tests/test_d2l_translate_score.py`: added offline D2L P3 tests.

### Stage 1: offline tests + full preflight

Commands:

```bash
python -m pytest pipeline/tests/test_d2l_translate_score.py -v
python -m pytest pipeline/tests/test_translate_runner.py pipeline/tests/test_context_builder.py pipeline/tests/test_llm_client.py pipeline/tests/test_d2l_builder.py -v
python -m pipeline.scripts.run_translate --db data/jobs/d2l_p1/memory.sqlite3 --profile technical_d2l_v1 --chapters introduction preliminaries linear_networks multilayer_perceptrons --configs S0 S1 --preflight-only
```

Output:

- `test_d2l_translate_score.py`: 5/5 passed.
- Related regression tests: 31/31 passed.
- Full 4-ch preflight:
  - resolved chapters: `d2l_introduction`, `d2l_preliminaries`, `d2l_linear_networks`, `d2l_multilayer_perceptrons`
  - windows: 169
  - blocks in windows: 1302
  - benchmark block types: `code=500`, `heading=283`, `image=26`, `label=1`, `math_block=26`, `prose=1019`
  - translatable block types: `heading=283`, `prose=1019`
  - registry injection stats: `raw_registry=1608`, `translation_eligible=747`, `preserve_count=239`, `hapax_dropped=622`
  - S0 prompt tokens min/avg/max: `533 / 1074.66 / 1783`
  - S1 prompt tokens min/avg/max: `645 / 1370.88 / 2258`
  - upper total all configs: `1,797,743` tokens `< daily cap 2,400,000`

### Stage 2: validity pilot `preliminaries`

Commands:

```bash
python -m pipeline.scripts.run_translate --db data/jobs/d2l_p1/memory.sqlite3 --profile technical_d2l_v1 --chapters preliminaries --configs S0 S1 --report data/reports/d2l_pilot_preliminaries_translate.json
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --chapters preliminaries --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_pilot_preliminaries.json
```

Pilot results:

- Windows: 45 S0 + 45 S1.
- Blocks translated: 348 S0 + 348 S1.
- JSON fail rate: `0.0000` for both.
- Actual tokens:
  - S0: `41,691` prompt + `33,627` completion = `75,318`
  - S1: `51,262` prompt + `33,425` completion = `84,687`
  - pilot total: `160,005`
- Pilot metrics:
  - B flat: S0 `0.6913` -> S1 `0.7896`
  - B recurring: S0 `0.6932` -> S1 `0.7983`
  - D: S0 `0.5964` -> S1 `0.6996`
  - A S1 registry TAR: `0.9273`
- Mechanical gate:
  - no passthrough translated: pass (`S0=true`, `S1=true`)
  - scope equals translation runs: pass (`S0=true`, `S1=true`)
  - preserve terms excluded from injection: pass
  - eligible terms all `occurrences_count >= 2`: pass
  - memory pack leak check: `0` packs with `code/math/image/label`
  - 5 manual passthrough audit samples were code/TOC/code snippets.

### Stage 3: full 4-ch run

Commands:

```bash
python -m pipeline.scripts.run_translate --db data/jobs/d2l_p1/memory.sqlite3 --profile technical_d2l_v1 --chapters introduction preliminaries linear_networks multilayer_perceptrons --configs S0 S1 --report data/reports/d2l_translation_translate.json
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --chapters introduction preliminaries linear_networks multilayer_perceptrons --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_translation_metrics.json
```

Full run results:

- `preliminaries` reused existing translations: 45 skipped windows per config.
- New calls: 124 S0 + 124 S1.
- Final translation rows: `1302` S0 + `1302` S1.
- JSON fail rate: `0.0000` for both.
- Full D2L cache totals (all P3 D2L calls including pilot):
  - S0: 169 calls, prompt min/avg/max `484 / 960.51 / 1582`, total `302,284` tokens.
  - S1: 169 calls, prompt min/avg/max `590 / 1259.33 / 2049`, total `352,970` tokens.
  - Total UTC 2026-06-14 translate cache usage: `655,254` tokens / `338` calls.
  - This is safely below the `2,400,000` guard and the 2.5M free daily pool.
- Full metrics (`data/reports/d2l_translation_metrics.json`):
  - B flat: S0 `0.7519` -> S1 `0.8172` (`2330` pairs)
  - B recurring: S0 `0.7520` -> S1 `0.8195` (`2294` pairs)
  - D: S0 `0.5930` -> S1 `0.7007` (`715` terms)
  - A S1 registry TAR: `0.9343` (`8594` pairs)
- Full scope/gate:
  - scope blocks: `1302` (`heading=283`, `prose=1019`)
  - passthrough blocks: `553` (`code=500`, `math_block=26`, `image=26`, `label=1`)
  - no passthrough translated: pass
  - scope equals translation runs: pass
  - preserve terms excluded from injection: pass
  - eligible terms all `occurrences_count >= 2`: pass
  - memory pack leak check: `0`

### Final verification

```bash
python -m pytest pipeline/tests/ -v
```

Output: `112 passed in 64.09s`.

Note: pytest emitted a Windows cleanup warning for `D:\temp\pytest-of-Snail\pytest-current`
permission denied after the tests completed. Test result is still pass.

### Notes for Claude review

- B and D both improve from S0 to S1, but neither saturates to 1.0. This matches the D2L design:
  gold is independent from the injected registry, and D is measured over recurring registry terms.
- A S1 is high but not 1.0 (`0.9343`), so Translator mostly follows canonical injection but still misses some
  injected terms under the current prompt/window design. This is not a Stage 2 mechanical failure, but Claude
  should inspect worst terms before calling the metric story final.
- D v1 has a known limitation recorded in the report: it can only detect drift among registry canonical/allowed
  Vietnamese forms; unseen synonyms are `undetected`.

## 6. Review *(Claude điền)* — PASS (2026-06-14)

**Verdict: PASS.** Claude tái kiểm độc lập TỪ SCORER + đọc toàn bộ code (`d2l_translate_score.py`, `context_builder.py`, `profiles.py`, test), tự chạy lại guard test — KHÔNG nhận số theo lời.

### Validity — sạch hết
- **scope chấm = scope dịch:** `scope_equals_translation_runs` S0/S1=true; `no_passthrough_translated` S0/S1=true; 1302 scope (heading 283 + prose 1019) ≡ translation_runs; 553 passthrough (code 500/math 26/image 26/label 1) bị loại khỏi CẢ tử lẫn mẫu B/D. Test `test_d2l_scorer_scope...`: gold trong `b_code` không vào mẫu (pairs=5).
- **gold KHÔNG bị bơm (Directional Lock):** `context_builder._load_terms`/`_glossary_items` chỉ đọc `glossary_entries` (registry); KHÔNG bao giờ chạm `eval_glossary_gold` hay `d2l_gold_variants.csv`. Gold + variants chỉ vào scorer (`_load_gold_targets`).
- **injection occ≥2 + role==translate + canonical-only:** test xanh — `agent → tác nhân` CÓ; biến thể `tác tử`, preserve `PyTorch`/`.shape`, hapax `exposes`(occ=1) đều KHÔNG vào prompt. Stats khớp: eligible 747, hapax_dropped 622, preserve 239 (Σ=1608).
- **S0 trung thực no-memory:** `test_d2l_s0_purity_check` xanh; đường S0 không gọi context_builder.
- **Test Claude TỰ chạy lại:** 5/5 guard pass (block-filter / technical-prompt / S0-purity / injection / scope-variants-B/D/A). CodeX full 112/112.
- **Token (gg):** preflight 4ch ~1.80M < 2.4M; thực 655,254 tok / 338 call UTC 2026-06-14; max prompt S1 2258 < cap 6000. Không phình.

### Headline (OCCURRENCE-WEIGHTED theo doctrine dd — KHÔNG dùng field `overall`)
| Thước | S0 | S1 | Gap |
|---|---|---|---|
| B TAR-vs-gold (flat, occ-weighted) | 0.7639 | **0.8320** | +0.068 |
| B recurring (occ-weighted) | 0.7640 | 0.8339 | +0.070 |
| D consistency (gold-free) | 0.5930 | **0.7007** | +0.108 |
| A TAR-vs-registry (chẩn đoán, occ-weighted) | — | 0.9436 | — |

- **B KHÔNG bão hòa (0.832 < 1.0)** + gap thật → đúng ưu thế D2L so với TI (ở TI, TAR bão hòa ~1.0 vì không có gold độc lập). Đây là số headline track kỹ thuật cho hội đồng.
- **D:** undetected 27→1, drift 264→213, consistent 424→501. B⊥D hoạt động đúng: `Regression`=hồi quy(82)+bài toán hồi quy(9)=drift; `Classification`/`AI` tương tự — đảo biến thể hợp lệ VẪN tính trôi (đúng nỗi lo GVHD "tác tử/tác nhân").
- *Trích số:* CodeX §5 dùng B field `overall` (0.752→0.817); doctrine dd headline = occurrence-weighted (0.764→0.832). Cùng câu chuyện, gap gần bằng nhau; §6 chốt field occ-weighted.

### A = 0.9343 overall / 0.9436 occ-weighted (CodeX gắn cờ) — KHÔNG phải lỗi, KHÔNG chặn PASS
A là CHẨN ĐOÁN (Translator nghe lời registry-Builder), không phải headline. Soi worst-terms:
- `AI` 70/80 miss — Translator giữ "AI" tiếng Anh (forms_used: AI 71, trí tuệ nhân tạo 10). Lựa chọn chính đáng, không phải lỗi.
- `example` 60/131, `set` 57/100, `label` 50/102 — lệch canonical-Builder vs cách dịch tự nhiên của Translator.
- Đây CHÍNH là thứ A sinh ra để lộ; injection KHÔNG hỏng (B/D tăng + 6 guard test xanh + ~35 term bơm/window). KHÔNG muốn A=1.0 — ép canonical mù quáng (vd "trí tuệ nhân tạo" mọi nơi thay vì "AI") sẽ HẠI B. A=0.93 = bám mạnh nhưng không mù → lành mạnh.

### Follow-up (KHÔNG chặn PASS)
1. **Snapshot translation_runs D2L** ra artifact bền (như TI `snapshot_runs.py`) — DB gitignored, cần để tái-chấm khi đổi thước (COMET ở EV-03).
2. **A worst-terms** (example/set/AI) = tín hiệu cho P4+ rà lại canonical Builder vài term + cân nhắc cho phép giữ acronym.
3. **D v1 limitation** (chỉ bắt drift trong canonical/allowed forms; synonym lạ = undetected) — đã disclose trong report; nâng ở EV-03.

