# TASK_HYG_02_literary_builder_recall_density_preflight — Bỏ cap recall→density audit + relation label + 4 process-guard (offline, review-gated)

- **Status:** DONE
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (mm) [chính], (ll) artifact-review-trước-chạy + 6-mục, (kk) payload bất đối xứng, (hh) injection dataset-aware, (gg) token-discipline | PROMPT_DESIGN
- **Branch/Commit:** local CodeX changes only; no commit/push per user request.

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

**Đã làm**
- Bump Literary Builder prompt `literary_builder_context_v2` -> `literary_builder_context_v3`.
- Bỏ câu cap di sản `"Aim for 5-20 glossary terms per substantial chapter."`; thay bằng luật recall-at-build: extract mọi term visible đạt termhood bar, không áp count cap.
- Thêm relation label ngắn trong `LiteraryBuilderContextPack`: `A<->B [relation]: addr_a / addr_b (state_label)`. Notes chỉ append khi có flag/label `address_shift`, `conflict`, hoặc `revealed_identity`.
- Render script HYG-02 không fallback sang DB frozen cho Builder chronology nữa. Chương N chỉ dùng artifact prepass của chương `<N`; chương đầu dùng registry rỗng.
- Render script sinh thêm density audit và full-set Builder preflight table.
- Thêm tests cho version v3, render chronology, cache prefix byte-identical, deterministic context render, density anomaly.

**1. Representative full prompt**
- Builder v3 full prompt: `data/reports/literary_builder_prompt_sample.txt`.
- Translator S1 full prompt giữ để đối chiếu: `data/reports/literary_translator_s1_prompt_sample.txt`.
- Index ngắn: `data/reports/literary_prompt_samples.txt`.
- Audit machine-readable: `data/reports/literary_builder_context_audit.json`.
- Full frozen registry snapshot để review, KHÔNG bơm nguyên vào prompt: `data/reports/literary_registry_snapshot.json`.
- Density audit: `data/reports/literary_builder_density_audit.json`.

**2. Context inclusion policy**
- Builder sample cho `treasure_island_ch03` dùng registry source:
  `prepass_artifacts_prior_chapters:[...data/prepass/treasure_island_pilot/treasure_island_ch02.json]`.
- Pack ch03: included 15 / excluded 16 / dropped_by_budget 0.
- Context pack token estimate: 266 / 600.
- Relation lines giờ có label xã hội ngắn, ví dụ:
  `ent_narrator<->ent_captain [thường xuyên ở chung nhà]: ông / cậu (wary_curiosity)`.
- Density audit từ artifact Builder:
  - ch02: glossary_count 15, density 5.2283 / 1k source tokens, hapax 11, status OK.
  - ch03: glossary_count 7, density 2.4230 / 1k source tokens, hapax 5, status OK.
  - status_counts: OK = 2.

**3. Token budget**
Builder preflight (0 API):

```text
chapter_id | source_tokens | context_pack_tokens | prompt_tokens | included/excluded/dropped | status
-----------------------------------------------------------------------------------------------------
treasure_island_ch02 | 2869 | 0 | 4009 | 0/0/0 | OK
treasure_island_ch03 | 2889 | 266 | 4403 | 15/16/0 | OK
```

- Threshold mapping implemented: OK <= 8000, WARN <= 12000, SPLIT_REQUIRED <= 20000, ABORT > 20000.
- Current max Builder prompt = 4403, well below OK threshold.
- Translator S1 sample still estimates 895 prompt tokens.

**4. Cache plan**
- Builder prompt version v3 is embedded in messages, so it participates in replay-cache key.
- `cache_friendliness.system_prefix_byte_identical = true` for ch02/ch03.
- System prefix sha256: `4a9b1c84fbde7274529409db8a5235bd879d9d993ae3b1e34ea822b03623f0b3`.
- Context pack render remains deterministic: sorted candidates, no timestamp/random.
- Render chronology guard prevents preview cache/key confusion from future DB state.

**5. Stop condition**
- HYG-02 made no API calls and did not re-baseline.
- Future Builder run must stop before API if:
  - prompt status is `ABORT`;
  - prompt status is `SPLIT_REQUIRED` while split executor is not implemented;
  - density audit returns `REVIEW_REQUIRED`;
  - preflight max prompt exceeds configured cap;
  - prompt/context tokens grow unexpectedly across chapters.

**6. Cost-quality report**
- HYG-02 is offline-only, so no quality scores changed and no token quota was spent.
- The next re-baseline task must still report S1-vs-S0 token delta and memory-pack percentage of prompt before any real run.
- Current evidence for opening review: Builder v3 prompt is bounded; removing cap does not affect injected prompt size because injection remains precision-at-inject.

**Files changed**
- `pipeline/prepass/prompt.py`
- `pipeline/prepass/literary_context.py`
- `pipeline/scripts/render_literary_prompts.py`
- `pipeline/tests/test_literary_builder_context.py`
- `data/reports/literary_prompt_samples.txt`
- `data/reports/literary_builder_prompt_sample.txt`
- `data/reports/literary_translator_s1_prompt_sample.txt`
- `data/reports/literary_builder_context_audit.json`
- `data/reports/literary_registry_snapshot.json`
- `data/reports/literary_builder_density_audit.json`

**Commands/output**

```text
python -m pytest pipeline\tests\test_literary_builder_context.py -v
=> 8 passed
```

```text
python -m pytest pipeline\tests -k "chronology or render_fidelity" -v
=> 1 passed, 119 deselected
```

```text
python -m pytest pipeline\tests -k "cache_prefix or deterministic" -v
=> 2 passed, 118 deselected
```

```text
python -m pipeline.scripts.render_literary_prompts --chapters 2,3 --density-out data/reports/literary_builder_density_audit.json --preflight-table
=> Builder prompt est tokens: 4403
=> Translator S1 prompt est tokens: 895
=> ch02 OK, ch03 OK
```

```text
python -m pytest pipeline\tests -k "d2l or registry or injection" -q
=> 30 passed, 90 deselected
```

```text
python -m pytest pipeline\tests -q
=> 120 passed in 75.47s
```

**Gotcha**
- Pytest exits 0 but Windows still prints the known post-test cleanup warning:
  `PermissionError: D:\temp\pytest-of-Snail\pytest-current`. This is not a failing assertion.

## 6. Review *(Claude điền)*

- **Verdict: PASS** (Claude, 2026-06-15 — tái kiểm ĐỘC LẬP từ diff + artifact + tự chạy lại test).

**Đã xác minh:**
1. **Scope giữ đúng:** không API, CodeX KHÔNG commit (working tree dirty), không re-baseline. **D2L không đụng** — regression `d2l/registry/injection` 30/30 PASS.
2. **Bỏ cap + bump v3:** diff `prompt.py` đổi `"Aim for 5-20 glossary terms"` → `"Extract every visible term that meets this bar; do not impose a count cap."`; `LITERARY_PROMPT_VERSION`→`v3`; test assert v3 trong prompt + "Aim for 5-20" vắng mặt. Termhood bar + negative examples giữ nguyên.
3. **Relation label:** `_compact_relation_label` (đọc relation/relation_type/role, trunc ≤36 ký tự theo word-boundary) + notes CHỈ khi cờ `address_shift`/`conflict`/`revealed_identity` (`_include_relation_notes`). Render `ent_narrator<->ent_captain [lodger/inn-boy]` xác nhận trong test + sample.
4. **Render-chronology guard CÓ THẬT (đóng lỗ HYG-01):** `_registry_for_builder_sample` dùng `prior_chapters = chapter_ids[:-1]`, nạp artifact chương <N, **RAISE nếu thiếu — từ chối fallback DB frozen**. Test khẳng định build ch03 thấy `admiral benbow inn` (ch02) NHƯNG KHÔNG thấy `black dog` (item của chính ch03); chương đầu → `empty_registry_first_chapter`. Đây là chỗ HYG-01 từng "đẹp giả vì thấy tương lai" — nay khóa bằng test.
5. **Cache-friendliness:** test assert system prefix BYTE-IDENTICAL giữa 2 chương khác nhau + render deterministic (cùng input→cùng output); báo cáo `cache_friendliness` được sinh.
6. **Density audit:** đủ field (`glossary_count`, `glossary_per_1k_source_tokens`, `hapax_count`, `category_distribution`, `sample_new_terms`, `density_anomaly`, `status`). Số thật: ch02 5.2283/1k (15 gloss), ch03 2.4230/1k (7 gloss) → đều OK (ch03 THẤP hơn, không jump). Test tổng hợp jump 2× → `REVIEW_REQUIRED`, `density_anomaly=True`, hapax đúng.
7. **Full-set preflight:** ch02 context 0 (chương đầu, registry rỗng — đúng chronology), ch03 context 266 incl15/exc16; status OK; prompt 4009/4403 < cap 6000.
8. **Test Claude tự chạy lại:** 8 (context) + 3 (chronology/cache/density) + 30 (regression) = PASS. `PermissionError D:\temp\pytest-current` = atexit cleanup temp Windows, exit 0, KHÔNG phải assertion fail.

**Ghi chú nhỏ (KHÔNG chặn):**
- context pack 266 tok (vs HYG-01 corrected 228) tăng do thêm nhãn relation — rẻ, hợp lý.
- density anomaly chỉ fire từ chương ≥2 (cần `previous_density`); đúng thiết kế. Chuẩn hóa per-1k-source-token là đúng (không dùng raw count) → bền ở quy mô sách.
- Split executor + cost-quality gate + near_miss đã đúng là OUT (không build) — kỷ luật scope tốt.

**Follow-up:** GATE còn đóng. Re-baseline TI S0/S1 dưới prompt v3 = **task kế**, mở sau khi (a) user duyệt prompt v3 (artifact đã trình) + (b) task đó kèm bảng cost-quality (S1−S0 token delta + memory-pack % prompt) theo LOCK (mm).6. HYG-02 (offline hygiene + guard) HOÀN THÀNH.

---

**GATE (LOCK mm/ll):** task REVIEW. KHÔNG re-baseline/S2 cho tới khi (a) user duyệt prompt render v3, (b) Claude review, (c) có bảng cost-quality (S1−S0 token delta + memory-pack % prompt) ở task re-baseline. HYG-02 chỉ mở cổng, không chạy.
