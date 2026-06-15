# TASK_HYG_01_literary_context_policy_v2 — Vệ sinh prompt/context/cost TI: bump translator v2 + LiteraryBuilderContextPack + render prompt review TRƯỚC S2/S3

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (ll) [chính], (kk) một-engine-nhiều-profile, (ii) scope=scope + base-prompt-ceiling, (hh) injection dataset-aware, (gg) token-discipline | PROMPT_DESIGN | RETRIEVAL_ARCHITECTURE
- **Branch/Commit:** (điền khi imple xong)

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

- **Representative full prompt:** (≥1 Builder + ≥1 Translator prompt thật/render đầy đủ — dán hoặc trỏ file)
- **Context inclusion policy:** (đưa gì vào pack, loại gì, theo luật nào)
- **Token budget:** (system / user / context / source / output — ước lượng/call)
- **Cache plan:** (prefix ổn định nào; kỳ vọng cache hit; cache-key gồm gì; version bump → miss có chủ đích)
- **Stop condition:** (ngưỡng prompt/call; preflight/ceiling kích hoạt khi nào)
- **Cost-quality report:** (sẽ điền sau khi chạy thật ở task kế — task này tới preflight; ghi ước lượng token/cost cho re-baseline)
- (kèm) Đã làm gì, file nào đổi, quyết định nhỏ + lý do; output các lệnh acceptance (dán nguyên văn); gotcha.

## 6. Review *(Claude điền)*

- **Verdict:** (trống)
- Findings: …
- Follow-up: …

---

**GATE (LOCK ll):** task này READY. **KHÔNG** chạy re-baseline S0/S1 hay S2/S3 cho tới khi (a) user duyệt prompt render (§3.3) **và** (b) Claude review thiết kế pack (giữ pattern hội tụ 3 bên user/CodeX/Claude). Render + preflight là điều kiện để mở cổng đó, không phải để chạy luôn.
