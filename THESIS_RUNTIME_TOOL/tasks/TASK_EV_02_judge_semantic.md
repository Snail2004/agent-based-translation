# TASK_EV_02_judge_semantic — Trục đánh giá ngữ nghĩa/văn phong: judge Gemini (pairwise A/B + GEMBA) + MATTR + hiệu chuẩn

- **Status:** READY
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

—

## 6. Review *(Claude điền)*

—
