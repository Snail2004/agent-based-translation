# TASK_EV_02_judge_semantic — Trục đánh giá ngữ nghĩa/văn phong: judge Gemini (pairwise A/B + GEMBA) + MATTR + hiệu chuẩn

- **Status:** DONE (PASS — Claude 2026-06-13; xem §6)
- **Refs:** THESIS_ARCHITECTURE_LOCK §6.2 (profile 4 trục — trục 2 "đúng nghĩa" + trục 4
  "đúng giọng"), §2.2 (judge = Gemini, KHÁC provider với translator GPT; backtranslate
  Gemini), changelog (z)/(z-bis)/(z-ter)/(aa) (TAR mù văn phong → judge là trục bù);
  changelog (r)/(s) (CẤM adaptation → tiêu chí no-omission/hallucination); mẫu hạ tầng
  client = P0-02 `llm_client.py` (transport injectable + replay cache + cost); bảng
  `evaluation_runs` (migration 003, KHÔNG bị freeze chặn — ghi eval thoải mái)
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

TAR/ECS (EV-01) đo nhất quán thuật ngữ/tên — **mù với "dịch đúng nghĩa & hay không"**.
EV-02 dựng trục bù: **LLM-judge cross-provider (Gemini)** chấm chất lượng + **MATTR**
(đa dạng từ vựng, tất định) + **khung hiệu chuẩn judge↔người** (không có nó thì số judge
vô giá trị khoa học). Đây là thước DUY NHẤT so được hệ-của-mình vs oracle về *chất lượng*
(không phải nhất quán — TAR không làm được, xem (z-bis)). Chạy pilot trên S0 vs S1
ch02+ch03 (thứ đang có); harness config-agnostic để tái dùng cho S2/S3/oracle sau.

## 2. Scope

**IN:**
1. `pipeline/agents/judge_client.py` — `JudgeClient(config, cache_path, transport=None)`
   mirror `llm_client.py`:
   - Model pin Gemini (config `pipeline/configs/judge_gemini.yaml`: model id Gemini
     trên AI Studio free tier — CodeX pin id thật đang dùng, ghi §5; temperature thấp;
     pricing nếu có / để 0 nếu free). **Cross-provider guard:** raise nếu model id
     chứa `gpt`/`o1`/`o3` hoặc trùng provider translator (judge GPT chấm GPT = CẤM §2.2).
   - `call(messages, response_format=json, tag)` → replay cache SQLite per-call (key =
     sha256(model+messages+temperature+response_format)); hit → 0 gọi mạng, 0 cost.
   - Key đọc `GEMINI_API_KEY`/`GOOGLE_API_KEY` từ env → fallback file `GEMINI-KEY.txt`
     repo root (gitignore — THÊM vào .gitignore); CẤM log key.
   - Transport thật = Gemini SDK; test = fake transport (KHÔNG mạng).
2. `pipeline/eval/judge.py` — logic chấm, 100% tách khỏi transport:
   - **Pairwise A/B (CHỦ LỰC):** `pairwise(source, vi_a, vi_b) ->` verdict.
     - Ẩn nhãn hệ; nhãn hiển thị ngẫu nhiên "Bản 1/Bản 2".
     - **Swap×2**: chấm 2 lần với thứ tự A/B đảo. Cùng winner cả 2 lần → win chắc;
       lệch nhau → **tie (low-confidence)** — đây chính là cơ chế bắt position-bias.
     - Output: winner ∈ {a, b, tie}, rationale ngắn, 2 verdict thô.
   - **GEMBA direct (PHỤ/CHẨN ĐOÁN, KHÔNG dùng kết luận chính — điểm tuyệt đối trôi):**
     `gemba(source, vi) ->` 4 tiêu chí 0–100: `adequacy` (đúng nghĩa nguồn),
     `fluency` (tự nhiên tiếng Việt), `style_voice` (giữ giọng), `fidelity_no_adddrop`
     (KHÔNG thêm/bớt ý — neo luật chống-adaptation (r)/(s)). + rationale.
   - **MATTR (tất định, 0 API):** `mattr(text, window=50) -> 0..1` đa dạng từ vựng;
     thuần Python, có giá trị tính tay được trong test.
3. `pipeline/eval/judge_calibration.py` — **bắt buộc, mảnh CodeX bỏ sót**:
   - `spearman(judge_scores, human_scores) -> rho` + `pairwise_agreement(judge_verdicts,
     human_verdicts) -> %` (Cohen-style agreement cho A/B).
   - `load_human_ratings(csv_path)` đọc file người chấm
     (`data/eval/human_ratings.csv`: block_id/scope_id, comparison, human_winner|human_score).
   - Báo cáo ghi rõ: judge **CHƯA hiệu chuẩn** nếu thiếu file người → mọi số judge gắn
     cờ `calibrated=false` (trung thực: không được trình bày như số chốt).
4. `pipeline/scripts/run_judge.py` — CLI:
   `--db ... --experiment exp_pilot_p3 --compare S0:S1 --chapters ch02 ch03 --out data/reports/judge_pilot.json [--human data/eval/human_ratings.csv]`
   - Đọc translation_runs 2 config → pairwise A/B mỗi block (scope='block') + 1 lần
     holistic mỗi chương (scope='chapter', cho tiêu chí style cần đoạn dài) + GEMBA mỗi
     bản + MATTR mỗi config/chương.
   - **Persist `evaluation_runs`** (run_id của bản được chấm, scope, scope_id,
     metric_name ∈ {pairwise_winner, gemba_adequacy, gemba_fluency, gemba_style_voice,
     gemba_fidelity, mattr}, metric_value, metric_version='judge_v1', judge_model,
     judge_rationale, ablation_label='S0_vs_S1'). Bảng này KHÔNG bị freeze chặn.
   - Report `data/reports/judge_pilot.json` (tracked): win-rate% (kèm số tie) +
     trung bình 4 tiêu chí GEMBA mỗi config + MATTR mỗi config + `calibrated` flag +
     (nếu có human) spearman ρ / agreement%.
5. Tests offline 100% `pipeline/tests/test_judge.py` + `test_judge_calibration.py`
   (fake transport, fixture — KHÔNG mạng).
6. `.gitignore`: thêm `GEMINI-KEY.txt`, `data/eval/human_ratings.csv` (dữ liệu người,
   không track cho tới khi chốt).

**OUT:** COMET/COMET-Kiwi (EV-03 — hạ tầng model neural nặng, cần kiểm EN-VI + domain);
MQM-lite/ACS discourse (EV-04, vào sâu arc xưng hô); backtranslation (chẩn đoán, pha sau);
THU THẬP dữ liệu người (user tự chấm ~20–30 cặp khi sẵn sàng — task này chỉ dựng KHUNG +
đọc CSV); S2/S3 (chưa tồn tại — harness phải config-agnostic để chạy được khi có).
KHÔNG sửa `eval/consistency.py`; KHÔNG ghi bảng memory frozen; KHÔNG đụng `app/`.

## 3. Spec — chi tiết chốt

- **Mù tuyệt đối:** prompt judge KHÔNG bao giờ chứa tên config/hệ ("S0"/"S1"/"oracle").
  Chỉ "Bản 1/Bản 2" gán ngẫu nhiên + seed ghi lại để tái lập.
- **Pairwise là kết luận chính; GEMBA-direct là phụ** (điểm tuyệt đối LLM trôi giữa
  item — chỉ dùng xem xu hướng + rationale, KHÔNG xếp hạng cuối bằng nó).
- **Win-rate phải kèm tie và n**: "S1 thắng 31% / hòa 60% / S0 thắng 9% trên 81 block"
  — KHÔNG rút gọn thành 1 số. (Kỳ vọng pilot: phần lớn HÒA vì S0≈S1 ở block không có
  hard-constraint — đó là kết quả TRUNG THỰC, không phải lỗi.)
- **Tính kết quả + cờ chưa-hiệu-chuẩn:** mọi báo cáo judge khi chưa có file người →
  `calibrated=false`; docstring + report nêu rõ "số judge chỉ thành kết luận sau khi
  ρ vs người đạt ngưỡng (đề xuất ρ ≥ 0.4 mới đáng tin)".
- Determinism: judge dùng temperature thấp + cache; chạy lại CLI = 0 token.

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_judge.py pipeline/tests/test_judge_calibration.py -v
# PHẢI PASS (fake transport, không mạng):
# 1. test_judge_cache_hit: 2 call giống nhau → transport gọi 1 lần, lần 2 from cache
# 2. test_cross_provider_guard: config model 'gpt-5.4-mini' → raise (judge cấm cùng GPT)
# 3. test_pairwise_blind_no_system_label: messages KHÔNG chứa 'S0'/'S1'/'oracle';
#    có 'Bản 1'/'Bản 2'
# 4. test_pairwise_swap_detects_position_bias: fake judge LUÔN chọn bản hiển thị đầu →
#    swap×2 phát hiện mâu thuẫn → verdict='tie' (low-confidence)
# 5. test_pairwise_consistent_win: fake judge luôn chọn bản X bất kể vị trí → winner=X
# 6. test_gemba_4_criteria_parsed: fake trả 4 điểm → parse + persist evaluation_runs đúng
# 7. test_mattr_handcomputed: text mẫu → MATTR khớp giá trị tính tay trong comment
# 8. test_calibration_spearman: judge vs human fixture → ρ đúng; thiếu human → calibrated=false
# 9. test_persist_evaluation_runs: hàng ghi đủ scope/metric_name/judge_model/ablation_label

# Chạy thật (cần GEMINI key; dán console + số vào §5):
python -m pipeline.scripts.run_judge --db data/jobs/treasure_island_p2/memory.sqlite3 --experiment exp_pilot_p3 --compare S0:S1 --chapters ch02 ch03 --out data/reports/judge_pilot.json
# - exit 0; in win-rate S1-vs-S0 (kèm tie + n), GEMBA 4 tiêu chí mỗi config, MATTR;
#   calibrated=false (chưa có file người) — ghi rõ trong report
# - chạy lại → cache hit toàn bộ, 0 token
# - CodeX soi tay 2–3 cặp rationale, dán §5 (judge có giải thích hợp lý không)

python -m pytest pipeline/tests/ -v   # toàn bộ vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

### 5.1 Files changed

- Added `pipeline/agents/judge_client.py`
  - Gemini judge client with injectable transport and SQLite replay cache.
  - Cross-provider guard rejects GPT/OpenAI model ids.
  - Reads key from `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GEMINI-KEY.txt`.
  - Supports `GEMINI_BASE_URL` / `GEMINI-BASE-URL.txt`.
  - Safety default: if the local key starts with `sk-`, the client uses
    `https://api.shopaikey.com` instead of sending a proxy key to Google official.
  - Added retry for 429 / 5xx / timeout / connection errors and 120s HTTP timeout.
  - Cache key includes `base_url` to avoid mixing official Gemini and proxy responses.
- Added `pipeline/configs/judge_gemini.yaml`
  - Pinned model: `gemini-2.5-flash`, `temperature=0.0`, pricing set to 0 because the
    current run used proxy credit, not official Google token pricing.
- Added `pipeline/eval/judge.py`
  - Pairwise blind A/B with swap x2.
  - GEMBA direct with four criteria: adequacy, fluency, style_voice,
    fidelity_no_adddrop.
  - Deterministic MATTR.
- Added `pipeline/eval/judge_calibration.py`
  - Spearman rho with average ranks for ties.
  - Pairwise agreement and human CSV loader.
  - Missing human file returns `calibrated=false`.
- Added `pipeline/scripts/run_judge.py`
  - Reads paired `translation_runs`.
  - Runs block pairwise, chapter pairwise, GEMBA per block/config, and MATTR per
    config/chapter.
  - Persists `evaluation_runs` with `metric_version='judge_v1'`.
  - Writes `data/reports/judge_pilot.json`.
- Added tests:
  - `pipeline/tests/test_judge.py`
  - `pipeline/tests/test_judge_calibration.py`
- Updated `.gitignore`
  - `GEMINI-KEY.txt`, `GEMINI-BASE-URL.txt`, `THESIS_RUNTIME_TOOL/data/eval/human_ratings.csv`.
- Updated `pipeline/requirements.txt`
  - Added `google-genai`.

No changes to `app/`, `AILAB_HANDOFF/`, `eval/consistency.py`, or frozen memory tables.

### 5.2 Offline validation

Command:

```bash
python -m pytest pipeline/tests/test_judge.py pipeline/tests/test_judge_calibration.py -v
```

Result:

```text
collected 11 items
11 passed in 2.17s
```

After proxy/base_url timeout-retry patch:

```bash
python -m pytest pipeline/tests/test_judge.py pipeline/tests/test_judge_calibration.py -q
```

Result:

```text
11 passed in 2.55s
```

Full suite:

```bash
python -m pytest pipeline/tests/ -q
```

Result:

```text
86 passed in 54.22s
```

Known Windows pytest cleanup warning still appears after successful exit:

```text
PermissionError: [WinError 5] Access is denied: 'D:\\temp\\pytest-of-Snail\\pytest-current'
```

Exit code was 0.

### 5.3 Real judge run

Official Gemini free-tier attempt failed before proxy setup:

```text
429 RESOURCE_EXHAUSTED ... limit: 5 ... model: gemini-2.5-flash
```

ShopAIKey smoke test:

```text
base_url=https://api.shopaikey.com
from_cache False
parsed {'ok': True, 'note': 'smoke'}
usage 16 11
cost 0.0
```

Full EV-02 pilot command:

```bash
python -m pipeline.scripts.run_judge --db data/jobs/treasure_island_p2/memory.sqlite3 --experiment exp_pilot_p3 --compare S0:S1 --chapters ch02 ch03 --out data/reports/judge_pilot.json
```

Operational notes:

- First long run timed out in the Codex tool before writing the report, but replay cache
  had already stored partial judge calls.
- One proxy call later hit `httpx.ReadTimeout`; client was patched to retry timeout and
  resume.
- Final report completed through replay cache + proxy resume.
- Proxy cache contains 310 `https://api.shopaikey.com` unique call entries plus 6 old
  pre-base-url entries that are not reused because the new cache key includes base URL.

Console summary:

```text
=== Pairwise ===
  n:    81
  S0: 10 (0.1235)
  S1: 12 (0.1481)
  tie: 59 (0.7284)
  calibrated: False

=== GEMBA ===
  S0: adequacy=70.7 fluency=72.9 style=69.5 fidelity=70.2 n=81
  S1: adequacy=54.4 fluency=54.9 style=53.1 fidelity=54.4 n=81

=== MATTR ===
  S0: mean=0.8641 n=2
  S1: mean=0.8617 n=2

Report written: data/reports/judge_pilot.json
```

`evaluation_runs` persistence check:

```text
judge_eval_rows 733
gemba_adequacy 162
gemba_fidelity 162
gemba_fluency 162
gemba_style_voice 162
mattr 2
pairwise_winner 83
```

### 5.4 Report interpretation

Report: `data/reports/judge_pilot.json`

Primary metric is pairwise blind A/B:

| Result | Count | Rate |
|---|---:|---:|
| S0 wins | 10 | 0.1235 |
| S1 wins | 12 | 0.1481 |
| Tie | 59 | 0.7284 |
| n | 81 | - |

Interpretation:

- This matches the expected S0-vs-S1 behavior: mostly ties. S1 is designed to enforce
  glossary constraints, not to improve literary style.
- Pairwise does show targeted wins where hard constraints fix real semantic/term errors,
  especially `treasure_island_ch02_b005` (`dead man's chest`).
- `calibrated=false` because no human ratings CSV was supplied. These judge numbers are
  diagnostic only until human calibration exists.

GEMBA direct is intentionally secondary. In this run it strongly favors S0 on direct
absolute scores while pairwise mostly returns ties. Treat this as evidence that direct
GEMBA scores are unstable/unreliable as headline numbers, consistent with the spec. Use
pairwise as the primary EV-02 signal.

MATTR is almost identical:

```text
S0 mean=0.8641
S1 mean=0.8617
```

This also supports the claim that S1 changes terminology adherence more than style.

### 5.5 Rationale spot-check

- `treasure_island_ch02_b005`: pairwise picks S1 because it translates `chest` as
  `Rương` instead of anatomical `ngực`, and uses `rượu rum`. This is a valid rationale
  and aligns with the TAR case study.
- `treasure_island_ch02_b013`: pairwise picks S0, mainly due to more faithful phrasing
  around the leg/amputation description and because S1 adds `bạc` to `fourpenny piece`.
  This is plausible and useful as a counterexample: S1 can improve glossary adherence
  while not always improving full semantic quality.
- Many ties are low-confidence caused by swap disagreement. This is expected and is the
  intended position-bias guard, not a failure.

### 5.6 Deviations / caveats

- Real run used a Gemini-compatible proxy (`https://api.shopaikey.com`) because official
  Gemini free-tier was rate-limited. The report therefore should not be presented as
  official Google billing/cost evidence. It is valid as a judge-output pilot with local
  prompt/response/cache logging.
- `cost_usd` is 0 in the judge client config because proxy credit accounting is not the
  same as official Gemini token pricing.
- Human calibration is not available yet. Any thesis-facing number must keep
  `calibrated=false` until human ratings are collected and Spearman/agreement are
  computed.

## 6. Review *(Claude điền)*

**Verdict: PASS** (Claude, 2026-06-13). Code đúng spec, an toàn, test thật-chạy-pass,
số headline đã tái tính độc lập từ DB và TRÙNG report. Không bịa. Kèm 4 finding bắt buộc
ghi nhận (không phải lỗi code — là tính chất dữ liệu + diễn giải) và 2 follow-up.

### 6.1 Tái xác minh độc lập (quy tắc sau vụ P3-01)

- **Pairwise tái tính từ `evaluation_runs` (KHÔNG đọc report JSON):** block-scope
  `S0(-1)=10 / tie(0)=59 / S1(+1)=12`, n=81 — **khớp report**. Số thật, không bịa.
- **Test mình tự chạy:** `test_judge.py` + `test_judge_calibration.py` → **11 passed**
  (traceback `WinError 5` cuối là dọn temp Windows của pytest, exit 0 — không phải fail).
  Đủ 9 test spec yêu cầu, có `test_cross_provider_guard` + `test_pairwise_swap_detects_position_bias`.
- **Audit bảo mật `judge_client.py`:** guard (`:307`) chặn `gpt/o1/o3/openai` VÀ bắt buộc
  có `gemini`; key KHÔNG bao giờ log (cache-key lưu `base_url`, không lưu key; thông báo
  lỗi không in key). Đạt.

### 6.2 Finding F1 — block-pairwise nhiễm bởi block rác (reviewer phát hiện, CodeX bỏ sót)

`treasure_island_ch02_b001`: source = `"I"` thực chất là **số chương La Mã "I"** (Chương I),
KHÔNG phải đại từ. S0 giữ `"I"` (đúng), S1 dịch `"Tôi"` (SAI). Judge lại chấm **S1 thắng**
vì tưởng bản kia "bỏ nguyên tiếng Anh" — judge không biết đó là heading.
→ Một phần trong 10/12 "thắng" là **nhiễu, thậm chí thắng ngược-hướng-đúng** trên block
1-token/heading/số. KHÔNG phải lỗi code (spec yêu cầu block-pairwise, CodeX làm đúng); là
tính chất dữ liệu mới lộ. **Headline phải dựa vào pairwise chương (holistic) + lọc block
dưới ngưỡng token tối thiểu**, và báo block-pairwise kèm caveat này.

### 6.3 Finding F2 — hướng GEMBA (S0 70 > S1 54) là ĐÁNH ĐỔI thật, không phải bug

GEMBA per-block CHẠY ĐÚNG: b005 S1=100 vs S0=30 (thắng "Rương Người Chết"); b002 S0=100
vs S1=95 (S1 bị trừ vì thêm "quán trọ"). Gap tổng nghiêng S0 vì các lựa chọn-có-chủ-đích
của S1 (viết hoa thuật ngữ canonical, mở rộng "quán trọ"/"rượu rum", ép dịch heading) bị
lăng kính tự-nhiên/fidelity trừ điểm trên các block thường, trong khi chỉ thắng đậm ở số ít
block thuật ngữ. → **Phát hiện học thuật trung thực: S1 mua nhất-quán-thuật-ngữ (TAR
0.42→1.0) bằng một chi phí tự-nhiên nhỏ mà TAR mù còn GEMBA thấy.** TUYỆT ĐỐI không lấy
"GEMBA nói S0 chất lượng hơn" làm headline (chưa hiệu chuẩn + gap do style-choice). Pairwise
là chính — đúng như đã chốt (z-ter).

### 6.4 Finding F3 — lệch spec: judge chạy qua proxy ShopAIKey (chấp nhận)

`judge_client.py:343` định tuyến key `sk-...` → `https://api.shopaikey.com` thay vì AI
Studio official (free-tier dính 429 5req/min). Cross-provider **vẫn giữ ở cấp model**
(gemini-2.5-flash ≠ gpt-5.4-mini — trọng số khác hãng, chỉ chung cổng thanh toán reseller).
Đạt yêu cầu §2.2. CỜ: `cost_usd=0` trong config → số chi phí proxy KHÔNG phải bằng chứng
giá token Google chính thức; báo cáo không được trình bày như chi phí Gemini official.

### 6.5 Finding F4 — 313 call/1 pilot S0-vs-S1 → bắt buộc sample (đã bàn 2 lượt trước)

GEMBA quét toàn bộ 81 block × 2 config = 162 call (một nửa lưu lượng) dù GEMBA chỉ là PHỤ.
Judge đắt ~20× dịch (dịch gom window, judge chấm block × swap × config). Chi phí ĐÁNH GIÁ
là chi phí nghiên cứu một-lần (cached), KHÔNG vào chi phí dịch/sản phẩm — phải tách bạch
trong báo cáo.

### 6.6 calibrated=false — trung thực

Report + warning + cờ `calibrated=false` đầy đủ. **Mọi số judge CHƯA được trích cho hội
đồng** cho tới khi có `human_ratings.csv` + Spearman ρ ≥ ngưỡng. Giữ nguyên kỷ luật này.

### 6.7 Follow-up (không chặn PASS)

1. **EV-02b (hoặc gộp lần chạy sau):** thêm `--sample N` + lọc block < ngưỡng token (bỏ
   heading/số/1-token khỏi block-pairwise); GEMBA chỉ chạy trên sample. → LOCK changelog.
2. **Thu thập human ratings ~20–30 cặp** (user chấm) → chạy calibration → gỡ cờ
   `calibrated=false`. Cho tới đó số chỉ là diagnostic.
