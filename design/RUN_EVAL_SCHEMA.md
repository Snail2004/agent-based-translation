# RUN / EVAL PERSISTENCE SCHEMA

> **Phạm vi hẹp — đọc kèm `RESEARCH_PLAN_V3.md`.**
> Doc này KHÔNG lặp lại V3. Memory T1–T7, 4 agents, hệ S0–S3, dataset D1–D6 và
> danh sách metric đã nằm trong V3 và đó là source of truth. Doc này chỉ bổ sung
> **3 GAP mà V3 không có schema cụ thể**: lớp lưu *run record + context snapshot +
> điểm eval* để **tái lập** và **chứng minh "tốt hơn baseline bằng số"**.
>
> Lý do tồn tại: theo chỉ thị GVHD (xem **Directional Lock** ở đầu V3), pipeline
> phải tự động từ 0 và phải đo được. "Nếu không lưu `context_bundle` + `run config`,
> sau này không chứng minh được vì sao bản dịch tốt hơn." → các bảng dưới phải có
> SẴN khi xây kiến trúc, dù scoring chạy sau.

---

## 0. Nguyên tắc nền (kế thừa Directional Lock của V3)

- Pipeline **autonomous từ 0**: không nạp annotation/bản dịch của người cho cuốn input.
- AI-LAB human gold = **eval-only** (reference + thước đo auto-extraction), nằm ở
  nhóm `reference_eval_only`, **cách ly tuyệt đối** khỏi mọi thứ translator nhìn thấy.
- **Trục align bất biến = `block_id`** trong `document_structure` (V3 §5.7/§10.4).
  Mọi output (S0..S3, gold, backtranslation) đều map về cùng `block_id`, kể cả khi
  translator nội bộ gộp/tách block khác đi.
- Kỷ luật provenance của AI-LAB transfer nguyên: mỗi mục auto-sinh phải ghi
  `model / prompt_version / confidence / version`; span resolve rồi
  `assert clean_text[span]==surface`. Khác AI-LAB ở chỗ **bỏ cổng người duyệt**.

---

## 1. GAP 1 — `context_bundle` (snapshot context cho từng block)

Ảnh chụp **chính xác thứ đã đưa vào Translator** cho 1 block trong 1 run. Đây là bằng
chứng cốt lõi để quy gán "nhờ cái gì mà hơn". Lưu **theo reference + hash**, không
nhân bản nguyên text memory.

| Trường | Mô tả |
|--------|-------|
| `bundle_id` | khóa chính |
| `run_id` | FK → `translation_runs` |
| `block_id` | FK → document_structure (trục align) |
| `config` | S0 \| S1 \| S2 \| S3 \| S3a..d (cho biết tầng nào được bật) |
| `memory_refs` | list id vào T1–T4 thực sự được retrieve (glossary_id, entity_id, summary_id, discourse_id…) |
| `retrieved_evidence` | list {source: T5/vector, ref_id, score} cho narrative/similar passages |
| `interpretation_brief` | brief JSON (nếu S3 gọi Narrative Agent), hoặc null |
| `resolved_prompt` | prompt cuối cùng đã render (text) |
| `prompt_hash` | hash của `resolved_prompt` để phát hiện drift / so trùng |
| `token_breakdown` | {level1, level2, level3, total_input} theo §11 token budget |
| `created_at` | timestamp |

> Ghi chú: với S0 `memory_refs`/`retrieved_evidence` rỗng — đó là *bằng chứng* baseline
> không có memory, không phải thiếu dữ liệu.

---

## 2. GAP 2 — `translation_runs` (output mỗi phương pháp × block)

| Trường | Mô tả |
|--------|-------|
| `run_id` | khóa chính (1 hàng = 1 block dịch bởi 1 config) |
| `experiment_id` | gom các run cùng 1 lần benchmark (E1…E5) |
| `block_id` | trục align |
| `config` | S0..S3 / S3a..d |
| `stage` | `draft` \| `revised` (revised = sau critic) |
| `prev_run_id` | nếu `revised`: trỏ về draft tương ứng |
| `output_text` | bản dịch |
| `model` | model translator |
| `prompt_version` | version prompt (đối chiếu PROMPT_DESIGN.md) |
| `temperature`, `seed` | để tái lập; **cố định xuyên S0..S3 trong 1 experiment** |
| `cost`, `latency_ms` | process metric (§11 Nhóm 6) |
| `created_at` | timestamp |

> **Ràng buộc ablation (quan trọng cho tính nhân quả):** trong 1 `experiment_id`,
> tất cả config dùng **cùng `model`, `temperature`, `seed`**; chỉ kiến trúc đổi.
> Nếu không, "hơn baseline" bị trộn biến và không bảo vệ được.

`critic_revision_runs` (CodeX nhóm 5) không tách bảng riêng: critic feedback dùng
**schema T7 QA** (V3 §4.7), revised output là 1 hàng `translation_runs` với
`stage=revised` + `prev_run_id`. Liên kết: T7.issue → `run_id` của draft bị bắt lỗi.
Thêm `retry_count` vào T7 issue nếu cần (max_retry=1, V3 §5.5).

---

## 3. GAP 3 — `evaluation_runs` (điểm, có nhãn ablation + judge meta)

| Trường | Mô tả |
|--------|-------|
| `eval_id` | khóa chính |
| `run_id` | FK → bản dịch được chấm |
| `scope` | block \| chapter \| book |
| `scope_id` | block_id / chapter_id / book_id tương ứng |
| `metric_name` | bleu \| chrf \| bertscore \| comet \| comet_kiwi \| gemba_da \| backtranslate \| tar_internal \| tar_gold \| ecs_internal \| ecs_gold \| mqm \| mhp |
| `metric_value` | điểm |
| `metric_version` | version metric/tokenizer (để tái lập; tokenizer VI cố định, §11 Nhóm 5) |
| `reference_id` | FK → `reference_eval_only` (null nếu metric reference-free, vd comet_kiwi/tar_internal) |
| `judge_model` | **PHẢI khác `translation_runs.model`**; null nếu không phải LLM-judge |
| `judge_rationale` | lý do của judge (lưu để audit, chống "judge tự khen") |
| `ablation_label` | nhãn so sánh: vd `S0_vs_S3`, `S3_vs_S3d` |
| `ci_low`, `ci_high` | bootstrap CI (bắt buộc khi n nhỏ — subset reference) |
| `created_at` | timestamp |

### 3.1. Tách đôi consistency metric (chốt với CodeX)

- `*_internal` (tar_internal, ecs_internal): so bản dịch vs **auto memory của chính nó**
  → đo *self-consistency*, chạy **toàn sách, không cần gold**. Đây là chỗ pipeline
  thắng baseline về cấu trúc.
- `*_gold` (tar_gold, ecs_gold): so registry auto vs **gold AI-LAB** → đo *correctness*
  của extraction, cần gold, eval-only.

→ Hệ quả: **memory quality ≠ translation quality**, hai trục đo riêng (trả lời case
"auto memory sai nhưng dịch vẫn hay"): translation quality = bleu/chrf/comet/mqm/mhp +
*_internal; memory quality = *_gold + precision/recall extraction vs gold.

---

## 4. `reference_eval_only` (gold, cách ly)

| Trường | Mô tả |
|--------|-------|
| `reference_id` | khóa chính |
| `block_id` | trục align về source |
| `target_text` | bản VI tham chiếu |
| `provenance` | `ailab_gold` (ưu tiên) \| `published` (phụ, khai báo leakage) |
| `leakage_risk` | low \| high — `published` của tác phẩm nổi tiếng = high |
| `subset_tag` | nhãn subset đã chốt TRƯỚC khi nhìn output (stratified) |

> **Bất biến cứng:** không bảng/luồng nào trong §1–§3 được đọc `target_text` của
> nhóm này trước/khi dịch. Chỉ `evaluation_runs` chạm tới, và chỉ ở pha chấm.

---

## 5. Đo "tốt hơn dịch thường" — bộ khung trả lời GVHD

- **Câu hỏi GVHD**: dịch xong làm sao biết hơn dịch thường? → **ablation ladder**
  trên cùng base model (V3 đã có S0..S3): S0 = "dịch thường" (1 phát, ít/không context).
- Reference-based (bleu/chrf/comet) chạy trên **reference subset** (n nhỏ → kèm CI);
  full-book output vẫn sinh đủ; **consistency `*_internal` chạy toàn sách**.
- Backtranslation (GVHD đề xuất) = **phụ/diagnostic**: per-block để soi, per-chapter
  trên sample cho faithfulness; không bao giờ là metric chính (round-trip nhiễu).
- LLM-judge: `judge_model` ≠ translator; blind A/B + shuffle thứ tự; validate bằng
  tương quan judge↔human trên sample nhỏ.

## 6. Làm ngay vs để sau (chống over-engineer)

| Làm NGAY khi xây kiến trúc | Để SAU (pha eval) |
|---|---|
| `block_id` alignment bất biến | cài đặt COMET/COMET-Kiwi |
| Ghi `context_bundle` (ref+hash) mỗi block | harness LLM-judge + rubric MQM |
| Ghi `translation_runs` (output+config+model+seed) | backtranslation pipeline |
| Slot bảng `evaluation_runs` + `reference_eval_only` (rỗng cũng được) | human eval round |

Mục tiêu trước mắt (GVHD): **dịch được end-to-end** trên 1 cuốn ngắn với S0 + S3,
miễn là alignment + 2 bảng ghi run/bundle đã sẵn để khỏi retrofit.

---

## 7. Map vào storage hiện tại (`schemas/memory_store_schema.sql`, schema_version 2)

Đã đối chiếu schema thật + `memory/store.py`. Kết luận: **gần hết đã có, chỉ thêm 3 bảng + 1 cột.**

| Nhóm (CodeX/§1–4) | Bảng hiện có | Trạng thái |
|---|---|---|
| source_alignment | `blocks` (PK `block_id`, có `order_index`/`chapter_id`/`scene_id`) | **REUSE** — `block_id` đã là khóa bất biến sẵn |
| runtime_auto_memory | `entities`,`mentions`,`glossary_entries`,`memory_items`,`scenes`,`events`,`speaker_turns` | **REUSE** (T1–T4). `status` mặc định `candidate`; runtime để auto, KHÔNG set `human_verified/locked` (§0.5) |
| context_bundle | `memory_packs` (pack_hash, prompt_version, payload_json, memory_refs_json, retrieval_debug_json) | **REUSE + thêm 1 cột `config`** (S0..S3). Đã được `save_memory_pack()` ghi thật |
| translation_runs | `translation_records` | ⚠️ **KHÔNG đủ** — PK = `block_id`, `record_translation()` dùng `ON CONFLICT(block_id) DO UPDATE` → **1 dịch/block, ghi đè**. Không chứa nổi S0..S3 × draft/revised cùng block |
| critic_revision_runs | (quality_json trong translation_records; chưa có bảng QA riêng) | gộp vào translation_runs (stage=revised) + quality_json; OK cho V1 |
| reference_eval_only | — | **CHƯA CÓ → bảng mới** |
| evaluation_runs | — | **CHƯA CÓ → bảng mới** |

**Delta cần làm (additive, không phá prototype đang chạy):**
1. **`translation_runs` (BẢNG MỚI)** — `run_id` PK, `experiment_id`, `block_id`, `config`, `stage`, `prev_run_id`, `output_text`, `model`, `prompt_version`, `temperature`, `seed`, `cost`, `latency_ms`. Giữ `translation_records` nguyên cho luồng prototype hiện tại (xem nó như "bản canonical/đang hiển thị" của 1 block); `translation_runs` chứa MỌI run thí nghiệm. Đây là thay đổi quan trọng nhất — nếu không có, không chạy được ablation.
2. **`evaluation_runs` (BẢNG MỚI)** — theo §3.
3. **`reference_eval_only` (BẢNG MỚI)** — theo §4.
4. **`memory_packs` + cột `config`** — để phân biệt bundle theo tầng S0..S3 (dùng `add_column_if_missing` như store.py đã làm cho `prompt_version`).
5. (Tùy) bump `schema_version` 2 → 3 trong `memory_meta`.

`human_feedback` + các `status` human-review: để nguyên, không dùng ở core (§0.6 future work).

---

## 8. Ranh giới

Doc này thuộc **thesis**. KHÔNG đưa vào `AILAB_HANDOFF`. Khi chi tiết mâu thuẫn với
`RESEARCH_PLAN_V3.md`, V3 là source of truth cho memory/agent/metric; doc này là
source of truth cho **lớp lưu run/eval**.
