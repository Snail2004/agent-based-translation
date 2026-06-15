# TASK_HYG_01_literary_context_policy_v2 — Vệ sinh prompt/context/cost TI: bump translator v2 + LiteraryBuilderContextPack + render prompt review TRƯỚC S2/S3

- **Status:** DONE
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (ll) [chính], (kk) một-engine-nhiều-profile, (ii) scope=scope + base-prompt-ceiling, (hh) injection dataset-aware, (gg) token-discipline | PROMPT_DESIGN | RETRIEVAL_ARCHITECTURE
- **Branch/Commit:** local CodeX changes only; no commit/push per user request.

## 1. Bối cảnh & mục tiêu *(Claude viết)*

TI (Treasure Island, profile `literary_v1`) đang ở bản CŨ, chưa khớp kỷ luật prompt/context/cost mới:

1. **Translator base prompt drift, version chưa bump.** Audit (LOCK gg/ll) phát hiện `LITERARY_SYSTEM_PROMPT` dùng ASCII hyphen `-` trong khi cache TI gốc dùng em-dash `—`; nội dung Builder literary byte-identical (KHÔNG stale), nhưng Translator lệch cosmetic mà `prompt_version` không đổi → số cũ vẫn hợp lệ nhưng vi phạm reproducibility-hygiene.
2. **Builder văn học vẫn bơm `REGISTRY_SO_FAR` qua `compress()`.** Xác minh `pipeline/prepass/registry.py`: `compress(max_tokens=600)` ĐÃ cap (`max_chars=max_tokens*4`, `append_capped` break khi vượt) → **KHÔNG cháy quota như D2L** (D2L nổ 2.5M vì registry hàng nghìn entry). Vấn đề thật = bound theo KÍCH THƯỚC, KHÔNG theo RELEVANCE-window: bơm entity/glossary ngoài window (nhân vật C / glossary G2 khi window chỉ A/B/G1) = token thừa + có thể RỚT item liên quan khi budget bị item-vô-quan ăn hết.

Theo LOCK (ll): **không chạy vội lấy số** — trước khi re-baseline S0/S1 hay xây S2/S3 phải làm sạch artifact prompt/context/cost, XUẤT prompt thật + preflight cost cho user review. Mục tiêu: pipeline TI bảo vệ được câu hỏi *"anh đưa gì vào prompt, vì sao, chi phí có đáng không?"*.

**Khung lại đúng mức độ:** đây là task **tối-ưu-relevance + vệ-sinh tái-lập + dựng artifact review**, KHÔNG phải vá khẩn cấp quota.

## 2. Scope

- **IN:**
  - Bump literary **translator** prompt → `literary_translator_v2` (chốt em-dash vs hyphen — chọn 1, ghi rõ; pin `prompt_version`; ghi changelog prompt).
  - Thiết kế + implement **`LiteraryBuilderContextPack`** (Option C): filtered/bounded continuity thay full `REGISTRY_SO_FAR` dump CHỈ cho nhánh Builder **văn học**.
  - **Render 2–3 prompt THẬT** (≥1 Builder + ≥1 Translator, ch02/ch03 TI) ra file cho user review TRƯỚC khi chạy.
  - **Preflight token/cost** cho re-baseline TI S0/S1 (offline / `--preflight-only`, 0 API): in token/call + tổng, max prompt/call < `prompt_token_cap`.
  - Điền §5 với **6 mục bắt buộc** (Representative full prompt / Context inclusion policy / Token budget / Cache plan / Stop condition / Cost-quality report).
- **OUT:**
  - KHÔNG chạy re-baseline S0/S1 **full** trong task này (chỉ tới render + preflight; chạy thật là task kế, SAU khi user duyệt prompt).
  - KHÔNG xây S2/S3 (sau).
  - KHÔNG đụng nhánh D2L (`technical_d2l_v1` đã đúng — omit registry; `build_d2l_terminology_messages` giữ "No prior registry").
  - KHÔNG đổi schema memory (FREEZE giữ; pack chỉ ĐỌC + lọc, không ghi).

## 3. Spec *(Claude viết)*

**3.1 Translator prompt v2** — `pipeline/translate/profiles.py`:
- Sửa `LITERARY_SYSTEM_PROMPT` khớp đúng bản cache TI gốc (em-dash `—`) HOẶC chốt ASCII hyphen rồi tái-baseline; chọn 1, ghi lý do ở §5.
- Thêm `prompt_version = "literary_translator_v2"`; đảm bảo version vào cache-key (re-run = cache miss có chủ đích, không lẫn số cũ).

**3.2 `LiteraryBuilderContextPack`** — thay đường full registry cho Builder văn học (hiện ở `pipeline/prepass/prompt.py` ~line 93 `f"REGISTRY_SO_FAR\n{registry_so_far_text}"` + `runner.py:336` fallback `registry.compress()`). KHÔNG đụng nhánh D2L. Thành phần (LOCK (ll).5):
  - **Matched entities** — chỉ entity có surface/alias xuất hiện trong chapter/window hiện tại.
  - **Matched glossary** — chỉ glossary có source surface trong text hiện tại.
  - **Active relations** — chỉ khi cả 2 entity xuất hiện HOẶC có dialogue/narrator cần xưng hô.
  - **Narrator card** — LUÔN đưa nếu first-person narration (continuity quan trọng).
  - **Recent carryover** — lượng nhỏ top-K / last-active entity+relation từ chương trước (bắt alias mới).
  - **Budget cap** — 300–600 token cho khối registry-context của Builder, có `dropped_by_budget`.
  - **Audit** — log `included` / `excluded` / `matched_by` / `dropped_by_budget` / `token_estimate`.
- Tái dùng pattern anchoring của Translator (`retrieval/context_builder.plan_anchors` — chỉ surface-match trong window). Builder mục tiêu = **continuity** (gộp/nối entity-relation/nhận alias-motif quay lại), KHÁC Translator (dịch đúng tại chỗ) → Builder KHÔNG full-dump, KHÔNG zero.
- Ghi audit ra `data/reports/literary_builder_context_audit.json` để user review include/exclude.

**3.3 Render script** — `pipeline/scripts/render_literary_prompts.py`: in ≥1 Builder prompt thật + ≥1 Translator prompt thật (ch02/ch03 TI) ra `data/reports/literary_prompt_samples.txt`, kèm audit include/exclude. KHÔNG gọi API.

**3.4 Preflight** — ước lượng token/call + tổng cho re-baseline S0/S1 TI 2 chương; in max prompt/call so với `prompt_token_cap` (`configs/llm_translate.yaml`); KHÔNG gọi API.

## 4. Acceptance criteria *(offline — chưa gọi LLM thật)*

```bash
# 1) offline tests cho LiteraryBuilderContextPack (anchoring + budget + audit)
python -m pytest THESIS_RUNTIME_TOOL/pipeline/tests/test_literary_builder_context.py -v   # PASS

# 2) guard: Builder văn học KHÔNG còn full registry dump; nhánh D2L vẫn omit registry
python -m pytest THESIS_RUNTIME_TOOL/pipeline/tests/ -k "registry or context_pack or injection" -v   # PASS

# 3) render prompt thật cho user review (0 API)
python THESIS_RUNTIME_TOOL/pipeline/scripts/render_literary_prompts.py --chapters 2,3 \
  --out data/reports/literary_prompt_samples.txt
#   → in ≥1 Builder + ≥1 Translator prompt ĐẦY ĐỦ + audit include/exclude

# 4) preflight token/cost (0 API)
python -m THESIS_RUNTIME_TOOL.pipeline.translate.run --preflight-only \
  --profile literary_v1 --chapters 2,3
#   → in token/call + tổng; max prompt < prompt_token_cap; KHÔNG gọi API
```
*(Tên module/đường dẫn CLI ở (3)–(4) là chỉ định; CodeX khớp với layout thực, ghi lệnh thật đã chạy vào §5.)*

## 5. Implementation notes *(CodeX điền — BẮT BUỘC đủ 6 mục dưới, LOCK (ll).6)*

**Đã làm**
- Bump Literary Translator prompt sang `s0_literary_translator_v2` / `s1_literary_translator_v2`; khôi phục em-dash `—` theo quyết định user để gần TI historical prompt hơn, nhưng version mới để cache-key/tái lập minh bạch.
- Bump Literary Builder prompt report/version sang `literary_builder_context_v2`; đổi user label từ `REGISTRY_SO_FAR` sang `REGISTRY_CONTEXT_PACK`.
- Thêm `pipeline/prepass/literary_context.py`: `LiteraryBuilderContextPack` lọc/bounded/auditable cho Builder văn học. D2L không đổi.
- Thêm script offline `pipeline/scripts/render_literary_prompts.py`.
- Thêm tests `pipeline/tests/test_literary_builder_context.py`; cập nhật expected prompt_version trong `test_translate_runner.py`.
- Rendered artifacts:
  - `data/reports/literary_prompt_samples.txt`
  - `data/reports/literary_builder_context_audit.json`

**1. Representative full prompt**
- File đầy đủ: `data/reports/literary_prompt_samples.txt`.
- Chứa 1 Builder prompt đầy đủ cho `treasure_island_ch03` và 1 Translator S1 prompt đầy đủ cho window `w_ch02_002`.
- Audit JSON máy đọc được: `data/reports/literary_builder_context_audit.json`.

**2. Context inclusion policy**
- Builder literary không còn dùng `registry.compress()` full-ish dump khi `mode="literary"`; runner tạo `LiteraryBuilderContextPack` từ registry hiện có.
- Include:
  - matched glossary: source surface xuất hiện trong chapter/window hiện tại;
  - matched entities: canonical/alias xuất hiện trong text;
  - narrator card: luôn include `ent_narrator` khi phát hiện first-person narration;
  - active relations: chỉ include nếu cả 2 endpoint entity đã visible/narrator trong pack; `recent_carryover` không tự kích hoạt relation;
  - recent carryover: top-K nhỏ cho entity không visible để chống miss alias, nhưng audit reason là `recent_carryover`.
- Exclude:
  - glossary/entity không xuất hiện trong text hiện tại;
  - relation có endpoint không nằm trong visible pack;
  - item vượt budget vào `dropped_by_budget`.
- Sample audit hiện tại: Builder pack included 25, excluded 16, dropped_by_budget 0, context token estimate 314/600.

**3. Token budget**
- Builder context pack budget: 600 rough tokens; sample used 314.
- Builder full prompt sample: 4,455 estimated prompt tokens. Phần lớn là source chapter text; context pack không phải nguyên nhân phình.
- Translator S1 sample window `w_ch02_002`: 895 estimated prompt tokens.
- Re-baseline translation preflight for TI ch02+ch03:
  - windows: 31; blocks: 81;
  - S0 prompt min/avg/max: 435 / 625.19 / 1520; prompt total 19,381;
  - S1 prompt min/avg/max: 475 / 758.32 / 1711; prompt total 23,508;
  - upper total with max output: S0 146,357; S1 150,484; all configs 296,841;
  - max prompt 1,711 < `prompt_token_cap` 6,000; upper total 296,841 < daily cap 2,400,000.

**4. Cache plan**
- No API call in HYG-01, so no new LLM cache rows expected.
- Future re-baseline will intentionally cache-miss versus TI historical translator runs because prompt_version changes from `s0_v1/s1_v1` to `s0_literary_translator_v2/s1_literary_translator_v2`.
- Cache key remains the P0 rule: canonical sha256 over model, messages, temperature, seed, reasoning_effort, response_format. The version string is embedded in messages, so it participates in the key.
- Builder context prompt also changes (`literary_builder_context_v2` + filtered pack), so any future Builder rerun is intentionally distinct.

**5. Stop condition**
- Do not run re-baseline/S2 until user approves rendered prompts and Claude reviews the pack.
- For future runs: abort if preflight prompt max exceeds `prompt_token_cap=6000`, or if upper token estimate exceeds `daily_token_cap=2400000`.
- If per-chapter/window prompt tokens grow superlinearly or context pack token estimate approaches budget unexpectedly, stop before API and inspect prompt/audit.
- API calls remain forbidden in this task.

**6. Cost-quality report**
- This task only renders/preflights; no quality score changed.
- Estimated upper quota for TI ch02+ch03 S0+S1 re-baseline is 296,841 tokens. This is an upper bound assuming max output on every window, not an actual expected bill.
- Quality hypothesis for next task: relevance-bounded Builder context should reduce irrelevant continuity tokens without removing visible glossary/entity/relation cues. This must be reviewed from prompt artifact before any run.

**Commands/output**

```text
python -m pytest pipeline\tests\test_literary_builder_context.py pipeline\tests\test_world_builder.py::test_runner_two_chapters_merges_registry pipeline\tests\test_translate_runner.py::test_runner_persists_pack_breakdown -v
=> 7 passed in 1.46s
```

```text
python -m pytest pipeline\tests -k "registry or context_pack or injection" -v
=> 10 passed, 107 deselected in 2.39s
```

```text
python -m pipeline.scripts.render_literary_prompts --chapters 2,3 --out data/reports/literary_prompt_samples.txt --audit-out data/reports/literary_builder_context_audit.json
=> Builder prompt est tokens: 4455
=> Translator S1 prompt est tokens: 895
=> Translator prompt version: s1_literary_translator_v2
```

```text
python -m pipeline.scripts.run_translate --db data/jobs/treasure_island_p2/memory.sqlite3 --profile literary_v1 --chapters ch02 ch03 --configs S0 S1 --preflight-only
=> Upper total all configs: 296841
=> S1 prompt_tokens min/avg/max: 475 / 758.32 / 1711
```

```text
python -m pytest pipeline\tests -q
=> 117 passed in 70.03s
```

**Gotcha**
- Running pytest from repo root fails import `pipeline`; run tests from `THESIS_RUNTIME_TOOL` or use the package path setup explicitly.
- Pytest exits 0 but Python prints a Windows temp cleanup warning for `D:\temp\pytest-of-Snail\pytest-current`; this is post-test cleanup, not a failing assertion.

## 6. Review *(Claude điền)*

- **Verdict: PASS** (Claude, 2026-06-15 — tái kiểm ĐỘC LẬP từ source + artifact + tự chạy lại test, không tin báo cáo).

**Đã xác minh:**
1. **Scope giữ đúng:** không gọi API, CodeX KHÔNG commit (working tree dirty), không re-baseline/S2. **D2L KHÔNG bị đụng** — runner route CHỈ `mode=="literary"` vào pack; nhánh D2L vẫn đi đường `D2L_REGISTRY_OMITTED_TEXT`. Guard 36/36 (`d2l/registry/injection/context_pack/prompt`) PASS.
2. **Translator v2:** diff `profiles.py` khôi phục em-dash `—` + bump `s0/s1_literary_translator_v2`; rendered samples đếm 18 em-dash, 0 ` - ` (clause hyphen) → drift đã sửa. Builder bump `literary_builder_context_v2`, label `REGISTRY_SO_FAR`→`REGISTRY_CONTEXT_PACK` (xác nhận `REGISTRY_SO_FAR` không còn trong sample).
3. **Relevance-anchoring CÓ THẬT (không phải chỉ đổi tên):** `literary_context.py` match surface nguồn (canonical+alias / source_term) trên text chương qua `normalize_apostrophe` (tái dùng fix apostrophe của P2-02); pack ch03 = **included 25 / excluded 16 / dropped_by_budget 0 / token 314 ≤ 600**. 16 item ngoài window bị loại = cơ chế lọc hoạt động. Relations chỉ vào khi cả 2 endpoint visible. Narrator-card-luôn-nếu-first-person là fallback đúng (ở đây Jim surface-visible → nằm trong MATCHED_ENTITIES, continuity còn nguyên).
4. **6 mục bắt buộc (LOCK ll.6):** đủ + thực chất ở §5. Preflight: max prompt S1 1711 < cap 6000; upper total all configs 296,841 < daily 2,400,000.
5. **Test Claude tự chạy lại:** `test_literary_builder_context.py`+`test_translate_runner.py` = 13 passed; subset d2l/registry/injection = 36 passed. `PermissionError` ở `D:\temp\pytest-current` = atexit cleanup temp của pytest trên Windows, exit 0, KHÔNG phải assertion fail.

**Ghi chú nhỏ (KHÔNG chặn, không cần fix ngay):**
- (a) `_has_first_person_narration` match `i/me/my/we/us/our` → có thể over-trigger narrator-card cho sách ngôi-thứ-3; vô hại với `literary_v1`=TI (ngôi thứ nhất). Nếu sau này thêm profile văn học ngôi-3 thì siết lại điều kiện này.
- (b) `ent_narrator` hardcode id: chạy đúng vì TI dùng đúng id + surface match phủ; nếu đổi convention id, fallback always-card lặng lẽ no-op (nhưng surface match vẫn đưa narrator vào). Chấp nhận.
- (c) Budget greedy-fill: priority hạ glossary/carryover (2/4) trước entities/relations (0/1) khi ép budget → chính sách continuity-first hợp lý; ghi rõ khi budget thực sự căng ở quy mô sách.

**Follow-up:** GATE vẫn đóng — **re-baseline S0/S1 dưới v2 là TASK RIÊNG**, chỉ mở sau khi (a) user duyệt prompt render (đã trình) **và** (b) review pack này (xong: PASS). HYG-01 (design + artifact + hygiene) HOÀN THÀNH.

---

**GATE (LOCK ll):** task này READY. **KHÔNG** chạy re-baseline S0/S1 hay S2/S3 cho tới khi (a) user duyệt prompt render (§3.3) **và** (b) Claude review thiết kế pack (giữ pattern hội tụ 3 bên user/CodeX/Claude). Render + preflight là điều kiện để mở cổng đó, không phải để chạy luôn.
