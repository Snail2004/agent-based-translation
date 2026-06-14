# TASK_P3_D2L_translate_score — Dịch S0/S1 trên 4 chương D2L (profile `technical_d2l_v1`) + chấm headline B (TAR-vs-gold) + D (consistency) + A chẩn đoán + judge sample

- **Status:** READY (spec hội tụ 3 bên — document_profile + base prompt D2L + scope-match; chạy **STAGED**: pilot 1 chương → full)
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

—

## 6. Review *(Claude điền)*

—
