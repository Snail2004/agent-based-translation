# TASK_HYG_02_literary_builder_recall_density_preflight — Bỏ cap recall→density audit + relation label + 4 process-guard (offline, review-gated)

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (mm) [chính], (ll) artifact-review-trước-chạy + 6-mục, (kk) payload bất đối xứng, (hh) injection dataset-aware, (gg) token-discipline | PROMPT_DESIGN
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

Hậu HYG-01 (Literary Builder đã chuyển sang `LiteraryBuilderContextPack` lọc relevance), chốt 3 bên (user/CodeX/Claude) trên 4 điểm + 4 process-guard → LOCK (mm). Task này HIỆN THỰC HÓA phần OFFLINE của (mm). Tất cả **0 API, không re-baseline, review-gated**. Mục tiêu: Builder văn học (a) không còn tự kìm recall bằng cap di sản, (b) có lưới an toàn density thay cho gold (văn học không có gold soi termhood), (c) relation mang đủ sắc thái xã hội, (d) khóa 4 guard tái-lập/cache/preflight để re-baseline sau này phòng-thủ-được.

## 2. Scope

- **IN:**
  - **Bỏ cap "Aim for 5-20 glossary terms"** trong prompt Builder văn học; giữ termhood bar + negative examples. Bump `literary_builder_context_v2`→`v3`.
  - **Density audit** sau build: glossary/chapter, glossary trên 1k source-token, hapax count, category distribution, 20 mục mới ví dụ; cờ bất thường nếu density ≥2–3× chương trước (chỉ fire từ chương ≥2); anomaly → status WARN/STOP, không auto-chạy Translator.
  - **Relation label**: thêm nhãn quan hệ xã hội ngắn vào dòng render (`[lodger/inn-boy]`); `notes` chỉ khi cờ `address_shift`/`conflict`/`revealed_identity`.
  - **Guard (a) version bump**: assert prompt version = `literary_builder_context_v3` trong test; version nằm trong cache-key.
  - **Guard (b) render-chronology test**: preview Builder chương N dùng registry từ ARTIFACT chương <N (không DB frozen merged); test fail nếu thấy item chỉ-chương-N trong context-pack của chính chương N.
  - **Guard (c) full-set preflight**: bảng `chapter_id | source_tokens | context_pack_tokens | prompt_tokens | included/excluded/dropped | status` cho TẤT CẢ chương định chạy; status theo ngưỡng `OK ≤8k / WARN 8–12k / SPLIT >12k / ABORT >20k`.
  - **Guard (d) cache-friendliness**: assert system+schema prefix byte-identical xuyên chương; context pack sort cố định (deterministic); không timestamp/random trong prompt.
  - Fold render-đúng-thời-điểm (ch02-artifact→ch03-source, included 15/excluded 16/228 tok) làm sample chuẩn.
- **OUT:**
  - **Executor SPLIT large-window + carry-in-progress** (chỉ dựng DETECTOR + status + ABORT; executor = task tương lai, TI không trip).
  - **Chapter-level consolidation** từ chunk-summaries (task riêng).
  - **`near_miss_candidates`** semantic report (để task S3-prep riêng).
  - **Re-baseline S0/S1 thật** + **cost-quality gate** (thuộc task re-baseline, cần số S0/S1).
  - D2L (không đụng); schema memory (FREEZE giữ).

## 3. Spec *(Claude viết)*

**3.1 `pipeline/prepass/prompt.py`** — xóa dòng `"Aim for 5-20 glossary terms per substantial chapter."` (hiện ~line 51); GIỮ termhood definition + negative examples (council/chart/bearing/parlor/basin/breakfast/stroke) + "Human/person entities belong in entities". Bump `LITERARY_PROMPT_VERSION = "literary_builder_context_v3"`.

**3.2 `pipeline/prepass/literary_context.py` — relation render** (`_relation_item`): thêm nhãn quan hệ ngắn từ `relation['relation']` (hoặc field role) → `A<->B [relation_label]: addr_a→b / addr_b→a (state_label)`; chỉ append `notes` khi `state_label`/flag ∈ {`address_shift`,`conflict`,`revealed_identity`} hoặc relation có cờ tương đương. Giữ token_estimate cập nhật.

**3.3 Density audit** — module/hàm mới (vd `literary_context.build_density_audit(...)` hoặc script): với mỗi chương output Builder, tính `glossary_count`, `glossary_per_1k_source_tokens`, `hapax_count`, `category_distribution`, `sample_new_terms` (≤20). Cờ `density_anomaly=True` nếu `glossary_per_1k` ≥ 2–3× chương liền trước (ngưỡng cấu hình; chỉ so từ chương ≥2). Ghi `data/reports/literary_builder_density_audit.json`. Anomaly → `status="REVIEW_REQUIRED"`, KHÔNG auto tiến Translator.

**3.4 Full-set preflight** — mở rộng `render_literary_prompts.py` (hoặc script preflight riêng): in bảng tất cả chương định chạy với cột status theo ngưỡng (mục 2). KHÔNG gọi API.

**3.5 Render-chronology guard** — sửa render để Builder chương N nạp registry từ artifact các chương <N (như `data/prepass/treasure_island_pilot/treasure_island_ch02.json`), KHÔNG từ DB frozen đã merge. Thêm test khẳng định bất biến.

**3.6 Cache-friendliness** — assert (test) rằng: với 2 chương khác nhau, prefix system+schema của Builder prompt là byte-identical; `LiteraryBuilderContextPack.render_context()` cho output ổn định khi input ổn định (sort cố định — đã có `sorted(...)`); không có timestamp/random trong message.

## 4. Acceptance criteria *(offline — 0 API)*

```bash
# 1) relation label + budget + audit + version v3
python -m pytest THESIS_RUNTIME_TOOL/pipeline/tests/test_literary_builder_context.py -v   # PASS

# 2) render-chronology guard (chương N chỉ thấy registry chương <N)
python -m pytest THESIS_RUNTIME_TOOL/pipeline/tests/ -k "chronology or render_fidelity" -v   # PASS

# 3) cache-friendliness (prefix byte-identical + deterministic sort)
python -m pytest THESIS_RUNTIME_TOOL/pipeline/tests/ -k "cache_prefix or deterministic" -v   # PASS

# 4) density audit report
python THESIS_RUNTIME_TOOL/pipeline/scripts/render_literary_prompts.py --chapters 2,3 \
  --density-out data/reports/literary_builder_density_audit.json
#   → json có glossary_count / glossary_per_1k_source_tokens / hapax_count / category_distribution / sample_new_terms + density_anomaly

# 5) full-set preflight bảng + status
python THESIS_RUNTIME_TOOL/pipeline/scripts/render_literary_prompts.py --chapters 2,3 --preflight-table
#   → bảng chapter_id|source_tokens|context_pack_tokens|prompt_tokens|inc/exc/drop|status; max prompt < cap; status OK cho TI

# 6) regression: D2L + injection không vỡ
python -m pytest THESIS_RUNTIME_TOOL/pipeline/tests/ -k "d2l or registry or injection" -q   # PASS
```
*(Đường dẫn/flag là chỉ định; CodeX khớp layout thực, ghi lệnh thật vào §5.)*

## 5. Implementation notes *(CodeX điền — BẮT BUỘC đủ 6 mục, LOCK (ll).6 + bảng preflight + cache report)*

- **Representative full prompt:** (Builder v3 thật/render đầy đủ — trỏ file)
- **Context inclusion policy:** (pack + density guard + relation label)
- **Token budget:** (system/user/context/source/output + bảng full-set preflight)
- **Cache plan:** (prefix byte-identical xuyên chương; sort cố định; version v3 trong cache-key)
- **Stop condition:** (status ABORT >20k; density_anomaly → REVIEW_REQUIRED; preflight > cap)
- **Cost-quality report:** (HYG-02 chưa chạy LLM — ghi ước lượng; gate thật ở task re-baseline)
- (kèm) file đổi + quyết định nhỏ + lý do; output lệnh acceptance nguyên văn; gotcha.

## 6. Review *(Claude điền)*

- **Verdict:** (trống)
- Findings: …
- Follow-up: …

---

**GATE (LOCK mm/ll):** task READY. KHÔNG re-baseline/S2 cho tới khi (a) user duyệt prompt render v3, (b) Claude review, (c) có bảng cost-quality (S1−S0 token delta + memory-pack % prompt) ở task re-baseline. HYG-02 chỉ mở cổng, không chạy.
