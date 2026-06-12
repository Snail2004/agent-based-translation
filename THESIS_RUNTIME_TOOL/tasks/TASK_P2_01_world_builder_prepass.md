# TASK_P2_01_world_builder_prepass — World Builder Agent: trích registry T1–T4 từ text trắng (pilot 2 chương TI)

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §2.1 (A1 World Builder: 1 agent/1 lượt đọc chương,
  TUẦN TỰ với registry-so-far nén; failure policy re-ask 1 lần), §2.2 (model stack,
  gpt-5.4-mini pin), §3 (T1–T4), §9 P2 (**go/no-go #1: JSON fail < 5%**), V3 Directional
  Lock §0 (tự xây từ text trắng — CẤM mọi dữ liệu oracle); bài học termhood:
  `TASK_EV_01_consistency_metrics.md` §6 finding #2
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

Task ĐẦU TIÊN gọi LLM thật của pipeline. World Builder đọc TUẦN TỰ từng chương
(text trắng đã strip — P1-01), mỗi chương 1 call, trích: T1 glossary candidates,
T2 entities + relations (kèm **xưng hô VI có pha** — showcase của đề tài), mention
surfaces (KHÔNG offset — Span Resolver P2-02 tính), T3 chapter summary, T4 motifs.
Output JSON validate bằng schema; sai → re-ask 1 lần. Pilot trên 2 chương đầu
Treasure Island → đo **json_fail_rate** (go/no-go #1). Span Resolver, persist SQLite,
FREEZE là P2-02 — task này chỉ sinh **artifact JSON tracked**.

## 2. Scope

**IN:**
1. `pipeline/configs/llm_prepass.yaml` — copy từ `llm_default.yaml`, đổi:
   `temperature: 0.2` (extraction cần ổn định hơn dịch), `max_output_tokens: 4096`
   (registry JSON/chương dài hơn 1 bản dịch block). Model/seed/reasoning_effort GIỮ pin.
2. `pipeline/prepass/schemas.py` — schema output chương (§3.2) + hàm
   `validate_chapter_output(obj) -> list[str]` (rỗng = hợp lệ; mỗi lỗi 1 dòng người đọc được).
3. `pipeline/prepass/prompt.py` — `build_messages(chapter, registry_so_far_text) -> list[dict]`:
   system (nhiệm vụ + kỷ luật termhood §3.3 + yêu cầu xưng hô VI + contract JSON) +
   user (registry-so-far nén + text chương có block marker §3.1).
4. `pipeline/prepass/registry.py` — state registry-so-far: `merge(chapter_output)` (union
   entity theo entity_id, glossary theo source_term casefold; ch sau ĐƯỢC reuse id cũ) +
   `compress() -> str` (one-liner, cap token §3.4).
5. `pipeline/prepass/runner.py` — `run_prepass(document_json_path, chapter_ids, client, out_dir)
   -> PrepassReport`: loop tuần tự, re-ask 1 lần khi JSON/schema fail (§3.5), ghi
   `<chapter>.json` + `run_report.json`.
6. `pipeline/scripts/run_prepass.py` — CLI:
   `--source data/sources/treasure_island/document.json --chapters ch01 ch02 --out data/prepass/treasure_island_pilot [--config pipeline/configs/llm_prepass.yaml] [--cache data/jobs/prepass_cache.sqlite3]`
   (match chapter theo SUFFIX chapter_id, vd `ch01` khớp `treasure_island_ch01`).
7. Tests offline 100% `pipeline/tests/test_world_builder.py` (fake transport — như P0-02).
8. **Chạy thật 2 chương TI** (cần `OPENAI_API_KEY`) → artifact tracked git:
   `data/prepass/treasure_island_pilot/{treasure_island_ch01.json, treasure_island_ch02.json, run_report.json}`.

**OUT:** Span Resolver + persist SQLite T1–T4 + FREEZE middleware (P2-02); Consolidation
call hợp nhất alias toàn sách (chạy khi full-book, không cần cho 2 chương); Chroma/
embedding (P2-03); mọi logic dịch. **CẤM TUYỆT ĐỐI:** đọc bất cứ gì từ `AILAB_HANDOFF/`
hay oracle (glossary.jsonl, entities.jsonl, annotations) — input DUY NHẤT của agent là
`data/sources/treasure_island/document.json` (đã strip, P1-01). KHÔNG đụng `app/`.

## 3. Spec

### 3.1. Render input chương (block marker)
Mỗi block 1 dòng, prefix block_id rút gọn (bỏ tiền tố doc):
```
[ch01_b001] To the hesitating purchaser...
[ch01_b002] Squire Trelawney, Dr. Livesey...
```
LLM tham chiếu block bằng các id này (trigger pha, motif, glossary block_ids). Map
id-rút-gọn ↔ block_id đầy đủ do code giữ; output file ghi block_id ĐẦY ĐỦ.

### 3.2. Schema output chương (JSON, response_format json_object)
```json
{
  "chapter_id": "treasure_island_ch01",
  "glossary_candidates": [
    {"source_term": "coracle", "proposed_target_vi": "thuyền thúng coracle",
     "do_not_translate": false, "category": "nautical|cultural|object|place|other",
     "block_ids": ["..."]}
  ],
  "entities": [
    {"entity_id": "ent_jim_hawkins", "canonical_source": "Jim Hawkins",
     "aliases_source": ["Jim"], "entity_type": "person|place|object|other",
     "proposed_target_vi": "Jim Hawkins", "aliases_target_vi": ["Jim"]}
  ],
  "relations": [
    {"a": "ent_jim_hawkins", "b": "ent_billy_bones", "relation": "chủ quán trọ - khách",
     "address_a_to_b_vi": "ông", "address_b_to_a_vi": "cậu nhóc",
     "state_label": "wary_curiosity", "trigger_block_id": null, "notes": ""}
  ],
  "mention_surfaces": [
    {"entity_id": "ent_jim_hawkins", "surfaces": ["Jim Hawkins", "Jim", "the boy"]}
  ],
  "chapter_summary_vi": "tóm tắt ≤150 từ tiếng Việt",
  "motifs": [{"note": "ghi chú văn phong/mô-típ lặp", "block_ids": ["..."]}]
}
```
Ràng buộc validate: field bắt buộc đủ + đúng type; `entity_id` snake_case prefix `ent_`;
mọi `entity_id` trong relations/mention_surfaces phải tồn tại trong `entities` HOẶC
registry-so-far (truyền danh sách id đã biết vào validator); list rỗng hợp lệ;
`trigger_block_id` null hoặc id có trong chương.

### 3.3. Kỷ luật termhood (bài học EV-01 — ghi THẲNG vào system prompt)
Term = từ/cụm cần dịch NHẤT QUÁN xuyên sách và có rủi ro trôi: thuật ngữ hàng hải,
sự vật/văn hóa đặc thù, tên riêng đồ vật/địa danh. **CẤM từ phổ thông** (kiểu council,
chart, terms, bearing — đưa ví dụ này vào prompt làm negative example). Định hướng
5–20 term/chương. Entity người KHÔNG vào glossary (đã có T2).

### 3.4. Registry-so-far nén (input cho chương sau)
- Entity: `ent_id | canonical_source (aliases) → target_vi` — 1 dòng/entity.
- Glossary: `source_term → target_vi` — 1 dòng/term.
- Relations: `a ↔ b: state_label, xưng hô a→b / b→a` — 1 dòng/cặp (state mới nhất).
- Cap **~600 token** (ước chars/4): vượt → giữ toàn bộ glossary + relations, cắt bớt
  entity theo số chương xuất hiện ít nhất. Chương 1: chuỗi rỗng + dòng "(registry trống —
  chương đầu)".

### 3.5. Runner + failure policy (đúng LOCK §2.1)
- Mỗi chương: build messages → `client.call(..., response_format={"type":"json_object"}, tag="prepass_<chapter>")`.
- `parsed_json` None HOẶC validator trả lỗi → **re-ask đúng 1 lần**: append message
  assistant (raw text) + user ("Output trước sai: <danh sách lỗi>. Trả lại JSON đúng
  schema, đủ field, không thêm lời."). Vẫn fail → chapter `status=failed`, ghi lỗi vào
  report, **đi tiếp chương sau** (registry không merge chương fail).
- `json_fail_rate = chương failed / tổng chương` (sau re-ask). Report ghi thêm:
  per-chapter {calls, prompt/completion/reasoning tokens, cost_usd, from_cache,
  counts (terms/entities/relations/mentions)}, tổng usage, `model`, `seed`,
  `system_fingerprint`.
- Replay cache (P0-02) → chạy lại CLI = 0 token, kết quả y hệt. Cache file đặt trong
  `data/jobs/` (đã gitignore).

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_world_builder.py -v
# PHẢI PASS (fake transport, KHÔNG mạng):
# 1. test_prompt_blocks_and_registry: messages chứa block marker [ch01_b001...] +
#    registry nén; KHÔNG chứa "AILAB"/"oracle"/đường dẫn handoff (Directional Lock)
# 2. test_schema_validation_catches: thiếu field, sai type, entity_id lạ trong
#    relations → mỗi lỗi 1 dòng; output hợp lệ → []
# 3. test_runner_two_chapters_merges_registry: fake trả JSON hợp lệ 2 chương →
#    2 file + run_report; prompt chương 2 CHỨA entity đã trích ở chương 1
# 4. test_reask_then_success: fake trả rác lần 1, JSON hợp lệ lần 2 → transport bị
#    gọi 2 lần, chương pass, json_fail_rate = 0, message re-ask chứa lỗi validate
# 5. test_failed_chapter_continues: fake trả rác cả 2 lần ở ch1 → ch1 failed,
#    runner vẫn chạy ch2, json_fail_rate = 0.5, registry ch2 KHÔNG chứa rác ch1
# 6. test_registry_compress_cap: registry nhiều entity → compress() ≤ cap ước lượng,
#    glossary + relations giữ nguyên vẹn

# Chạy thật (CodeX chạy khi có OPENAI_API_KEY, dán console vào §5):
python -m pipeline.scripts.run_prepass --source data/sources/treasure_island/document.json --chapters ch01 ch02 --out data/prepass/treasure_island_pilot
# - exit 0; json_fail_rate <= 0.05 (GO/NO-GO #1); 3 file ghi ra (2 chapter + report)
# - report đủ usage/cost/model/seed/fingerprint; chạy lại lần 2 → from_cache toàn bộ
# - CodeX spot-check bằng mắt, ghi vào §5: số term/entity/relation từng chương;
#   ch01 PHẢI có Jim (narrator) + Billy Bones/"the captain"; relations có xưng hô VI;
#   glossary KHÔNG dính từ phổ thông lộ liễu

python -m pytest pipeline/tests/ -v   # toàn bộ pipeline tests vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

—

## 6. Review *(Claude điền)*

—
