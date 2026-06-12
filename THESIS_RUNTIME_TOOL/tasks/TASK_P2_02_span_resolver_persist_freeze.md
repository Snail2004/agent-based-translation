# TASK_P2_02_span_resolver_persist_freeze — Fix prompt P2-01 + re-run pilot + Span Resolver + persist T1–T4 + FREEZE

- **Status:** REVIEW
- **Refs:** THESIS_ARCHITECTURE_LOCK §2.1 (Span Resolver: code tính offset, LLM không
  đếm), §3 (bảng T1–T4: `glossary_entries`, `entities`, `mentions`, `entity_relations`,
  `memory_items`), §3.3 (quy tắc FREEZE), §9 P2; **6 findings bắt buộc vá:
  `TASK_P2_01_world_builder_prepass.md` §6**; schema: `pipeline/memory/schema_v2_base.sql`
  + `migrations/003_thesis_runs.sql`
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

Khép Phase P2 trên pilot: vá 6 findings của review P2-01 (nặng nhất: prompt hardcode
"Jim Hawkins" = tri thức ngoài văn bản — đã xác minh "Jim" không có trong text ch02),
re-run pre-pass ch02+ch03 bằng prompt sạch, rồi **đưa registry vào SQLite**: Span
Resolver string-match offsets, persist T1–T4 đúng schema, và **FREEZE** — chốt chặn
"World Builder chỉ ghi TRƯỚC freeze" thành ràng buộc cấp DB (trigger), không phải lời hứa.

## 2. Scope

**IN:**

### 2a. Vá prompt + runner (findings P2-01 §6)
1. `prompt.py` — **gỡ toàn bộ hardcode Treasure Island/Jim Hawkins**. Rule generic mới:
   - Narrator ngôi 1: tạo entity `ent_narrator`, `canonical_source` CHỈ từ bằng chứng
     trong text ("the narrator" nếu chưa lộ tên). Khi chương sau lộ tên thật → **re-emit
     entity với CÙNG entity_id** (`ent_narrator`), cập nhật canonical_source/aliases.
     Quy tắc re-emit áp dụng cho MỌI entity registry có alias/tên mới (case Bill/Billy).
   - `mention_surfaces` bắt buộc cho MỌI entity xuất hiện trong chương — kể cả entity
     registry cũ quay lại.
   - Surfaces/aliases **CẤM đại từ thuần** (I, me, my, he, she, him, her, they...).
   - `canonical_source` = MỘT tên sạch, cấm ghép kiểu "X / Y".
   - `address_*_vi` phải thuần tiếng Việt (cấm "sonny / cậu bé").
   - Termhood: thêm negative examples mới (parlor, basin, breakfast table, stroke) +
     rule "đồ vật gia dụng/từ sinh hoạt thường ngày KHÔNG phải term".
2. `schemas.py` — validator thêm: cảnh báo lỗi nếu canonical_source chứa "/", nếu
   surface/alias là đại từ thuần (danh sách EN cố định trong code), nếu address_vi
   chứa chuỗi ASCII-only dài (heuristic lộ tiếng Anh — chỉ warning, không fail).
3. `runner.py` — report ghi **usage THẬT** (đọc từ `result.usage` kể cả khi
   from_cache — cache đã lưu usage gốc) + giữ `cache_hits` + thêm
   `incremental_cost_usd` (chỉ call không cache). Hết cảnh usage=0 trong file tracked.
4. **Re-run pilot** `ch02 ch03` (prompt mới → cache miss, ~12k token — chấp nhận) →
   artifacts mới ĐÈ file tracked cũ.

### 2b. Span Resolver (code thuần, 0 LLM)
> Prior art: `app/backend/services/annotation_flow.py::_resolve_surface` (AI-LAB) giải
> bài KHÁC — resolve TỪNG candidate kèm context, mơ hồ → trả "unresolved" cho NGƯỜI xử.
> Thesis cần liệt kê HẾT match của surface, không người xử — VIẾT MỚI, KHÔNG import
> (quyết định LOCK changelog (x)); được phép mượn test case/ý tưởng từ đó.
5. `pipeline/prepass/span_resolver.py`:
   - Input: document.json (stripped) + các artifact chương (`data/prepass/.../*.json`).
   - Match trên `clean_text` (NFC, `re.IGNORECASE`, word-boundary
     `(?<!\w)...(?!\w)` — CÙNG triết lý matching với `eval/consistency.py`):
     - glossary term → occurrences: (term, block_id, char_start, char_end).
     - entity surfaces (đã lọc đại từ) → mentions: (entity_id, block_id, surface,
       char_start, char_end). Offsets tính trên clean_text gốc (KHÔNG casefold text
       khi lấy offset — casefold có thể đổi độ dài; chỉ dùng IGNORECASE).
   - Output: `ResolvedSpans` + **coverage report**: term 0-occurrence, entity
     0-mention (để soi LLM bịa term/surface lệch chính tả) — đưa vào build report.

### 2c. Persist T1–T4 (mapping đúng schema)
6. `pipeline/prepass/persist.py` — `build_memory(db_path, document_json_path, prepass_dir, freeze=True) -> BuildReport`:
   - Nếu bảng `blocks` trống → tự gọi `load_document` (P1-01). DB đã frozen → **từ chối
     chạy** (raise, message rõ; rebuild = file DB mới).
   - **T1** glossary (merge các chương theo source_term casefold) → `glossary_entries`:
     glossary_id=`gl_<slug>`, source_term, target_term=proposed_target_vi,
     do_not_translate (0/1), term_type=category, status='approved',
     occurrences_count + last_block_id từ resolver.
   - **T2** entities → `entities`: entity_id giữ nguyên, canonical_source (sạch),
     canonical_target=proposed_target_vi, aliases_source_json (LỌC đại từ — lớp chặn
     thứ 2 sau prompt), aliases_target_json, entity_type, first/latest_block_id từ
     resolver, status='approved'.
   - mentions từ resolver → `mentions`: mention_id=`m_<block>_<entity>_<n>`,
     mention_type='name', char_start/char_end, confidence mặc định.
   - **Relations → `entity_relations` TIMELINE** (điểm tinh nhất — làm đúng):
     đọc TOÀN BỘ artifact theo thứ tự chương (KHÔNG dùng registry in-memory — nó chỉ
     giữ state cuối); gom states per cặp (a,b) chuẩn hóa thứ tự; sort theo
     `order_index` toàn cục của trigger_block_id (null → block đầu chương đó);
     `valid_from_block_id` = trigger; `valid_to_block_id` = block đứng NGAY TRƯỚC
     valid_from của state kế (theo order_index); state cuối → null (mở).
     address_policy_json = `{"a_to_b": ..., "b_to_a": ...}`; relation_type=relation;
     notes giữ nguyên.
   - **T3** chapter_summary_vi → `memory_items`: memory_type='chapter_summary',
     scope='chapter', chapter_id, content=summary, status='approved'.
   - **T4** motifs → `memory_items`: memory_type='motif', scope='chapter', content=note,
     payload_json=`{"block_ids": [...]}`, status='approved'.
7. **FREEZE** — `pipeline/memory/migrations/004_freeze_triggers.sql` + helper
   `pipeline/memory/freeze.py`:
   - memory_meta key `memory_frozen` ('0'/'1'); `freeze_memory(conn)` set '1' + key
     `frozen_at` (ISO time). KHÔNG có hàm unfreeze (muốn mở = sửa tay, có chủ đích).
   - Trigger BEFORE INSERT/UPDATE/DELETE trên `glossary_entries`, `entities`,
     `mentions`, `entity_relations`, `memory_items`:
     `WHEN (SELECT value FROM memory_meta WHERE key='memory_frozen')='1'
      → RAISE(ABORT, 'memory frozen (LOCK §3.3)')`. Enforce 1 chỗ, cấp DB.
8. `pipeline/scripts/build_memory.py` — CLI:
   `--source data/sources/treasure_island/document.json --prepass data/prepass/treasure_island_pilot --db data/jobs/treasure_island_p2/memory.sqlite3 --freeze`
   → in BuildReport JSON + ghi bản tracked `data/reports/memory_build_pilot.json`
   (counts per bảng + coverage warnings + frozen_at).

**OUT:** Consolidation call toàn sách (khi full-book); Chroma/embedding (P2-03);
mọi logic dịch (P3). KHÔNG đọc AILAB_HANDOFF; KHÔNG đụng `app/`; KHÔNG log key.

## 3. Spec — chi tiết chốt

- Đại từ lọc (danh sách cố định, casefold): i, me, my, mine, myself, you, your, he,
  him, his, she, her, hers, it, its, we, us, our, they, them, their.
- Slug glossary_id: lowercase, không dấu cách (`gl_admiral_benbow_inn`).
- Resolver KHÔNG match surface là substring của surface dài hơn đã match cùng vị trí
  (vd "Jim" trong "Jim Hawkins" — lấy match dài nhất, tránh mention đôi cùng offset).
- Idempotent TRƯỚC freeze: build lại trên DB chưa frozen → DELETE dữ liệu prepass cũ
  của doc rồi ghi lại (nội dung cuối giống hệt). DB đã frozen → raise như §2c.6.
- BuildReport: {doc_id, glossary, entities, mentions, relations, memory_items,
  coverage: {terms_zero_occurrence: [...], entities_zero_mention: [...]}, frozen_at}.

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_span_resolver.py pipeline/tests/test_persist_freeze.py -v
# PHẢI PASS (fixture tự tạo, giá trị tính tay trong comment):
# 1. test_resolver_word_boundary_offsets: "rum" match đúng offset, KHÔNG match "rumor";
#    IGNORECASE; offset đúng trên text có “quote”/tiếng Việt
# 2. test_resolver_longest_surface_wins: "Jim Hawkins" và "Jim" cùng vị trí → 1 mention
#    dài nhất
# 3. test_resolver_coverage_flags: term bịa (0 occurrence) + entity 0 mention → vào report
# 4. test_persist_mapping: fixture artifact 2 chương → đúng hàng glossary_entries/
#    entities/mentions/memory_items; đại từ bị lọc khỏi aliases; status='approved'
# 5. test_relations_timeline: cặp (a,b) có 2 states ở 2 trigger khác nhau →
#    valid_from/valid_to nối đúng theo order_index, state cuối valid_to=NULL
# 6. test_freeze_blocks_writes: sau freeze, INSERT/UPDATE/DELETE vào cả 5 bảng → raise;
#    bảng ngoài T1-T4 (vd translation_runs) vẫn ghi được
# 7. test_build_refuses_frozen_db: chạy build lần 2 trên DB frozen → raise message rõ
# 8. test_prompt_no_external_knowledge: prompt KHÔNG chứa "Treasure Island"/"Jim"/
#    "Hawkins"; có rule ent_narrator + re-emit + cấm đại từ trong surfaces

# Re-run pre-pass với prompt sạch (key như P2-01; dán console vào §5):
python -m pipeline.scripts.run_prepass --source data/sources/treasure_island/document.json --chapters ch02 ch03 --out data/prepass/treasure_island_pilot
# - json_fail_rate <= 0.05; artifact ch02 KHÔNG chứa "Jim"/"Hawkins" trong entities
#   (narrator chưa lộ tên); ch03 ĐƯỢC PHÉP cập nhật tên narrator nếu text lộ "Jim";
#   captain PHẢI có surface mới Bill/Billy trong mention_surfaces ch03;
#   report usage THẬT khác 0

# Build memory + freeze (offline):
python -m pipeline.scripts.build_memory --source data/sources/treasure_island/document.json --prepass data/prepass/treasure_island_pilot --db data/jobs/treasure_island_p2/memory.sqlite3 --freeze
# - exit 0; report counts > 0 cho cả 5 nhóm; coverage warnings in ra (soi tay, ghi §5);
#   data/reports/memory_build_pilot.json được ghi (tracked)

python -m pytest pipeline/tests/ -v   # toàn bộ pipeline tests vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

- Implemented prompt fixes in `pipeline/prepass/prompt.py`: removed all book/person
  hardcode, switched first-person narration to generic `ent_narrator`, added re-emit
  guidance, required mention surfaces for returning registry entities, banned plain
  pronouns in aliases/surfaces, banned slash-joined canonical names, and tightened
  termhood negatives (`parlor`, `basin`, `breakfast table`, `stroke`).
- Extended `pipeline/prepass/schemas.py`: canonical_source containing `/` fails;
  plain pronouns in `aliases_source` or `mention_surfaces` fail; English-looking
  address tokens can be emitted as warnings.
- Updated `pipeline/prepass/runner.py`: tracked report records original usage/cost
  even for cache hits, while `incremental_cost_usd` records only non-cache cost.
- Added pure-code span resolver in `pipeline/prepass/span_resolver.py`: NFC text,
  `re.IGNORECASE`, Unicode word-boundary `(?<!\w)...(?!\w)`, offsets on original
  `clean_text`, longest surface wins at the same start offset, and coverage warnings
  for zero-occurrence terms/entities.
- Added SQLite persist/freeze: `pipeline/prepass/persist.py`,
  `pipeline/memory/freeze.py`, `pipeline/memory/migrations/004_freeze_triggers.sql`,
  and `pipeline/scripts/build_memory.py`. Build auto-loads `blocks` if empty,
  refuses frozen DBs, persists T1-T4, and freezes via DB triggers.
- Added offline tests in `pipeline/tests/test_span_resolver.py` and
  `pipeline/tests/test_persist_freeze.py`.

Validation:

```bash
python -m pytest pipeline/tests/test_span_resolver.py pipeline/tests/test_persist_freeze.py -v
# 8 passed in 4.37s

python -m pipeline.scripts.run_prepass --source data/sources/treasure_island/document.json --chapters ch02 ch03 --out data/prepass/treasure_island_pilot
# json_fail_rate: 0.0
# ch02: passed, calls=1, terms=15, entities=9, relations=5, mentions=9, motifs=3
# ch03: passed, calls=1, terms=7, entities=5, relations=5, mentions=5, motifs=3
# total_usage: prompt_tokens=8232, completion_tokens=5786, reasoning_tokens=1082
# cost_usd=0.01363, incremental_cost_usd=0.01363, cache_hits=0

python -m pipeline.scripts.build_memory --source data/sources/treasure_island/document.json --prepass data/prepass/treasure_island_pilot --db data/jobs/treasure_island_p2/memory.sqlite3 --freeze
# glossary=22, entities=10, mentions=165, relations=10, memory_items=8
# coverage terms_zero_occurrence=["sailor's clasp-knife", "the Dead Man's Chest"]
# coverage entities_zero_mention=[]
# frozen_at=2026-06-12T14:53:14.210345+00:00

python -m pytest pipeline/tests/ -v
# 37 passed in 12.76s
```

Spot-checks:

- `treasure_island_ch02.json` no longer contains `Jim` or `Hawkins`; narrator is
  `ent_narrator` with canonical_source `the narrator`.
- `treasure_island_ch03.json` may contain `Jim` because the text reveals it; the
  captain has Bill/Billy/Billy Bones surfaces.
- ch03 glossary no longer contains `parlor`, `basin`, `breakfast table`, or `stroke`.
- `data/jobs/treasure_island_p2/memory.sqlite3` is under gitignored `data/jobs/`;
  tracked build report is `data/reports/memory_build_pilot.json`.

## 6. Review *(Claude điền — 2026-06-12)*

- **Verdict: PASS — Phase P2 (pilot) HOÀN THÀNH** (deliverable LOCK §9 P2 đạt:
  registry T1–T4 trên 2 chương TI trong SQLite, json_fail 0.0 < 5%) → tag `P2-done`.
- Tự chạy lại: 37/37 pipeline tests; đọc đầy đủ span_resolver.py / persist.py /
  freeze.py / 004_freeze_triggers.sql; xác minh artifact + report + DB thật.
- **Bằng chứng tự học (Directional Lock — quan trọng cho luận văn):** ch02 narrator
  = `ent_narrator` / "the narrator", KHÔNG còn Jim/Hawkins (đã grep); ch03 re-emit
  CÙNG entity_id với canonical "Jim" — tên học TỪ VĂN BẢN (b033 "Jim," says he);
  captain nhận surfaces mới Bill / Billy Bones / Mr. Bones / Master Billy Bones.
  Finding #1 (P2-01) đóng đúng cách.
- **FREEZE probe trên DB thật (không chỉ tin test):** INSERT glossary_entries và
  UPDATE entities → `IntegrityError: memory frozen (LOCK §3.3)`; translation_runs
  vẫn ghi được; bonus phát hiện schema có CHECK `config IN ('S0'..'SLC')` hoạt động.
  FREEZE giờ là ràng buộc vật lý, kể cả UI app/ sau này cũng không sửa được memory.
- **Relations timeline xác minh trên DB:** valid_from/valid_to nối đúng XUYÊN chương
  theo order_index (vd captain↔narrator: wary_curiosity ch02_b011 → ch03_b004, rồi
  tense_obedience ch03_b005 → NULL). Đây chính là cấu trúc xưng hô động cần cho S3.
- **Root cause 2 term zero-occurrence (tự điều tra):** văn bản chỉ dùng apostrophe
  cong U+2019 (’), LLM viết ASCII (') → "sailor's clasp-knife" / "the Dead Man's
  Chest" không match. Coverage report bắt đúng như thiết kế. **Follow-up nhỏ cho task
  kế:** normalize apostrophe (’→') hai phía trong `_find_word_boundary_matches` (và
  cân nhắc đồng bộ trong `eval/consistency.py` để hai thước đo cùng triết lý).
- Findings nhỏ không chặn: (1) dedupe mention theo (block, char_start) — surface lồng
  nhau khác điểm bắt đầu (vd "Benbow" trong "Admiral Benbow") vẫn có thể đếm đôi,
  hiếm và vô hại ở pilot; (2) reasoning_tokens tăng 182→1.082 do prompt chặt hơn —
  chi phí vẫn ~$0,014/2 chương, không đáng kể; (3) persist gán confidence cố định
  (0.7/0.75) — chấp nhận, LLM không trả confidence đáng tin.
- Kết quả persist: glossary 22, entities 10, mentions 165, relations 10 (timeline),
  memory_items 8 (2 summary + 6 motif). Memory ĐÓNG BĂNG, sẵn sàng làm nguồn
  retrieval cho P3/P4.
