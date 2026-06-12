# TASK_P4_01_embeddings_chroma — Embedding client + dựng 3 Chroma collections từ memory frozen

- **Status:** DONE
- **Refs:** THESIS_ARCHITECTURE_LOCK §4.1 (3 collections, metadata bắt buộc, TM chỉ
  nhận bản pass Critic, embed vế EN làm khóa), §4.2 (text-embedding-3-large, ghi
  model+dim vào memory_meta), §5.2 (EN-keyed retrieval); DB frozen P2
  `data/jobs/treasure_island_p2/memory.sqlite3`; mẫu hạ tầng client = P0-02
  (`llm_client.py`: transport injectable + replay cache)
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

Mở màn P4. Trước khi có Context Builder (P4-02), phải có tầng vector: embedding client
(pin model, cache, cost) + 3 Chroma collections dựng từ memory frozen + nguồn sạch.
Thuần hạ tầng — 0 quyết định thiết kế mới (mọi thứ đã khóa §4). Chi phí thật ~$0,01
cho pilot 2 chương.

## 2. Scope

**IN:**
1. `pipeline/agents/embedding_client.py` — `EmbeddingClient(config, cache_path, transport=None)`:
   - Model pin `text-embedding-3-large` (config yaml riêng
     `pipeline/configs/embedding.yaml`: model, dimensions=3072, pricing input 0.13/1M,
     batch_size 64). Cấm alias.
   - `embed(texts: list[str]) -> list[list[float]]` — batch theo batch_size; replay
     cache SQLite riêng (cùng pattern P0-02): key = sha256(model + dimensions + text),
     cache theo TỪNG text (không theo batch — đổi batch không vỡ cache); hit → không
     gọi transport, không cộng cost.
   - Usage/cost track như P0-02 (`usage_daily` cùng file cache db của embedding).
   - Transport thật = OpenAI `client.embeddings.create`; test dùng fake transport
     (vector deterministic từ hash text — KHÔNG mạng).
2. `pipeline/retrieval/chroma_store.py` — Chroma persistent client tại
   `data/jobs/<job>/chroma/` (gitignored):
   - `build_index(conn, embedding_client, chroma_path, doc_id, chapter_ids) -> IndexReport`:
     - **`similar_passages`**: 1 block = 1 vector, text = `blocks.text` (EN, các chương
       được chọn), metadata `{doc_id, chapter_id, block_id, kind:'passage'}`.
     - **`narrative_motifs`**: 1 memory_item (motif + chapter_summary) = 1 vector,
       text = content, metadata `{doc_id, chapter_id, block_id: block_start, kind:
       'motif'|'chapter_summary', memory_id}`.
     - **`translation_memory`**: TẠO RỖNG + hàm `add_tm_entry(en_text, vi_text,
       metadata)` — embed vế EN làm khóa, VI nằm trong payload metadata; docstring ghi
       rõ: CALLER chỉ được add bản đã pass Critic (S3 dùng sau, §4.1).
     - Ghi `embedding_model` + `embedding_dimension` vào `memory_meta` lần index đầu
       (memory_meta KHÔNG bị trigger freeze chặn — đã xác minh P2-02); lần sau model/dim
       lệch → **raise** (đổi model = re-index toàn bộ, §4.2).
     - Idempotent: chạy lại → upsert theo id cố định (`block_id` / `memory_id`),
       số vector không nhân đôi.
   - `query_similar(client_or_collection, text, k=5, where=None)` +
     `query_motifs(...)` + `query_tm(..., chapter_window: list[str])` — TM filter
     metadata theo danh sách chương (3–5 chương gần nhất do caller đưa, §4.1).
3. `pipeline/scripts/build_index.py` — CLI:
   `--db data/jobs/treasure_island_p2/memory.sqlite3 --chroma data/jobs/treasure_island_p2/chroma --chapters ch02 ch03`
   → in IndexReport + ghi `data/reports/index_build_pilot.json` (tracked): counts per
   collection, model, dimension, tokens, cost_usd. Key đọc env → fallback
   `API-KEY.txt` root (như P2/P3, cấm log key).
4. `chromadb` vào `pipeline/requirements.txt` (pin major version đang cài, ghi §5).
5. Tests offline 100% `pipeline/tests/test_embedding_client.py` +
   `pipeline/tests/test_chroma_store.py` (fake transport; chroma chạy local persistent
   trong tmp_path — không mạng).

**OUT:** Context Builder / Query Planner / Reranker / Coverage Checker (P4-02);
mọi inject vào prompt dịch (P4-02+); Brief/Critic (P4-04); re-index toàn sách (P6).
KHÔNG ghi gì vào 5 bảng memory frozen; KHÔNG đụng `app/`, AILAB_HANDOFF.

## 3. Spec — chi tiết chốt

- Embedding input = text NFC (không casefold — embedding cần text tự nhiên).
- Chroma collection tạo với `metadata={"hnsw:space": "cosine"}`.
- ID vector: `similar_passages` dùng block_id; `narrative_motifs` dùng memory_id;
  `translation_memory` dùng `tm_<block_id>_<config>` (caller đưa).
- IndexReport: {passages, motifs, tm, model, dimension, embed_tokens, cost_usd,
  cache_hits, skipped_existing}.
- Sanity pilot: passages = số block ch02+ch03 (= 81), motifs = số memory_items (= 8).

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_embedding_client.py pipeline/tests/test_chroma_store.py -v
# PHẢI PASS (fake transport, không mạng):
# 1. test_embed_cache_per_text: 2 text, gọi 2 lần (lần 2 thêm 1 text mới) →
#    transport chỉ nhận text chưa cache; kết quả ổn định
# 2. test_embed_batching: 130 texts, batch_size 64 → transport gọi 3 lần đúng nhóm
# 3. test_model_pin: config model alias "latest" → raise ngay từ config
# 4. test_build_index_counts_and_metadata: fixture DB nhỏ → đúng số vector mỗi
#    collection; metadata đủ {doc_id, chapter_id, block_id, kind}
# 5. test_index_idempotent: build 2 lần → số vector không đổi (upsert)
# 6. test_model_mismatch_raises: memory_meta đã ghi model khác → raise re-index
# 7. test_tm_add_and_scope_query: add_tm_entry 3 chương → query_tm với
#    chapter_window 2 chương → chỉ trả vector trong scope
# 8. test_query_similar_topk: query trả đúng k láng giềng gần nhất (fake vectors
#    có khoảng cách kiểm soát được)

# Chạy thật (cần key; embedding ~17k tokens ≈ $0,01; dán console vào §5):
python -m pipeline.scripts.build_index --db data/jobs/treasure_island_p2/memory.sqlite3 --chroma data/jobs/treasure_island_p2/chroma --chapters ch02 ch03
# - exit 0; passages=81, motifs=8, tm=0; memory_meta có embedding_model + dimension
# - report tracked ghi ra; chạy lại lần 2 → cache_hits 100%, cost incremental = 0
# - Smoke query thật (CodeX chạy, dán kết quả): query_similar("the old captain at
#   the inn") → top-5 phải chứa block nói về captain/quán trọ (soi bằng mắt, ghi §5)

python -m pytest pipeline/tests/ -v   # toàn bộ vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

### 5.1. Files changed

- `pipeline/agents/embedding_client.py`
  - Added `EmbeddingConfig`, `EmbeddingClient`, per-text SQLite replay cache, `usage_daily`,
    OpenAI embeddings transport, NFC normalization, batch handling, and session usage.
  - Cache key = sha256 of `{model, dimensions, text}`; cache hits do not call transport and
    do not increment usage/cost.
  - Enforces exact model pin `text-embedding-3-large`; aliases/wrong models raise at config load.
- `pipeline/configs/embedding.yaml`
  - `model: text-embedding-3-large`, `dimensions: 3072`, `batch_size: 64`,
    `pricing.input: 0.13`.
- `pipeline/retrieval/chroma_store.py`
  - Added persistent Chroma client, `build_index`, `IndexReport`,
    `query_similar`, `query_motifs`, `query_tm`, and `add_tm_entry`.
  - Collections use `metadata={"hnsw:space": "cosine"}`.
  - `similar_passages`: id=`block_id`, document=`blocks.text`, metadata includes
    `{doc_id, chapter_id, block_id, kind:"passage"}`.
  - `narrative_motifs`: id=`memory_id`, document=`memory_items.content`, metadata includes
    `{doc_id, chapter_id, block_id:block_start, kind, memory_id}`.
  - `translation_memory`: created empty by build; `add_tm_entry` embeds EN source and stores VI
    in metadata payload. Docstring explicitly says caller may only add translations that passed
    Critic.
  - Writes `embedding_model` and `embedding_dimension` to `memory_meta`; mismatches raise with
    re-index message. This only touches `memory_meta`, not frozen T1-T4 tables.
  - Resolves CLI chapter suffixes (`ch02`, `ch03`) to DB IDs
    (`treasure_island_ch02`, `treasure_island_ch03`).
- `pipeline/scripts/build_index.py`
  - CLI for build + report + smoke `query_similar("the old captain at the inn")`.
  - Reads `OPENAI_API_KEY`; fallback reads root `API-KEY.txt` without logging the key.
- `pipeline/tests/test_embedding_client.py`
  - Offline fake transport tests for per-text cache, batching, and model pin.
- `pipeline/tests/test_chroma_store.py`
  - Offline Chroma tmp-path tests for counts/metadata, idempotency, mismatch guard, TM scoped
    query, and controlled top-k query.
- `pipeline/requirements.txt`
  - Added `chromadb>=1,<2` after installing local `chromadb==1.5.9`.
- `data/reports/index_build_pilot.json`
  - Tracked report from the second build run: 81 passages, 8 motifs, 0 TM, cache_hits=89,
    incremental cost 0.

No changes to `app/` or `AILAB_HANDOFF/`.

### 5.2. Test output

```text
python -m pytest pipeline/tests/test_embedding_client.py pipeline/tests/test_chroma_store.py -v

collected 8 items
pipeline/tests/test_embedding_client.py::test_embed_cache_per_text PASSED
pipeline/tests/test_embedding_client.py::test_embed_batching PASSED
pipeline/tests/test_embedding_client.py::test_model_pin PASSED
pipeline/tests/test_chroma_store.py::test_build_index_counts_and_metadata PASSED
pipeline/tests/test_chroma_store.py::test_index_idempotent PASSED
pipeline/tests/test_chroma_store.py::test_model_mismatch_raises PASSED
pipeline/tests/test_chroma_store.py::test_tm_add_and_scope_query PASSED
pipeline/tests/test_chroma_store.py::test_query_similar_topk PASSED

8 passed in 10.35s
```

```text
python -m pytest pipeline/tests/ -v

collected 68 items
68 passed in 48.71s
```

Windows printed the known pytest temp cleanup warning after the pass:
`PermissionError: [WinError 5] Access is denied: 'D:\temp\pytest-of-Snail\pytest-current'`.
This occurs after pytest reports success and did not change the exit code.

### 5.3. Real build output

First run:

```text
python -m pipeline.scripts.build_index --db data/jobs/treasure_island_p2/memory.sqlite3 --chroma data/jobs/treasure_island_p2/chroma --chapters ch02 ch03

=== Index Report ===
passages:         81
motifs:           8
translation_mem:  0
model:            text-embedding-3-large
dimension:        3072
embed_tokens:     6243
cost_usd:         $0.000812
cache_hits:       0
existing_ids:     0
Report written:   data\reports\index_build_pilot.json

=== Smoke query_similar ===
query: the old captain at the inn
1. treasure_island_ch02_b002 chapter=treasure_island_ch02 distance=0.428676 text=At the Admiral Benbow
2. treasure_island_ch02_b009 chapter=treasure_island_ch02 distance=0.479008 text=“Well, then,” said he, “this is the berth for me. Here you, matey,” he cried to the man who trundled the barrow; “bring up alongside and hel
3. treasure_island_ch02_b006 chapter=treasure_island_ch02 distance=0.488887 text=in the high, old tottering voice that seemed to have been tuned and broken at the capstan bars. Then he rapped on the door with a bit of sti
4. treasure_island_ch02_b014 chapter=treasure_island_ch02 distance=0.510881 text=But though I was so terrified by the idea of the seafaring man with one leg, I was far less afraid of the captain himself than anybody else
5. treasure_island_ch02_b010 chapter=treasure_island_ch02 distance=0.522154 text=And, indeed, bad as his clothes were, and coarsely as he spoke, he had none of the appearance of a man who sailed before the mast, but seeme
```

Second run:

```text
python -m pipeline.scripts.build_index --db data/jobs/treasure_island_p2/memory.sqlite3 --chroma data/jobs/treasure_island_p2/chroma --chapters ch02 ch03

=== Index Report ===
passages:         81
motifs:           8
translation_mem:  0
model:            text-embedding-3-large
dimension:        3072
embed_tokens:     0
cost_usd:         $0.000000
cache_hits:       89
existing_ids:     89
Report written:   data\reports\index_build_pilot.json

=== Smoke query_similar ===
query: the old captain at the inn
1. treasure_island_ch02_b002 chapter=treasure_island_ch02 distance=0.428676 text=At the Admiral Benbow
2. treasure_island_ch02_b009 chapter=treasure_island_ch02 distance=0.479008 text=“Well, then,” said he, “this is the berth for me. Here you, matey,” he cried to the man who trundled the barrow; “bring up alongside and hel
3. treasure_island_ch02_b006 chapter=treasure_island_ch02 distance=0.488887 text=in the high, old tottering voice that seemed to have been tuned and broken at the capstan bars. Then he rapped on the door with a bit of sti
4. treasure_island_ch02_b014 chapter=treasure_island_ch02 distance=0.510881 text=But though I was so terrified by the idea of the seafaring man with one leg, I was far less afraid of the captain himself than anybody else
5. treasure_island_ch02_b010 chapter=treasure_island_ch02 distance=0.522154 text=And, indeed, bad as his clothes were, and coarsely as he spoke, he had none of the appearance of a man who sailed before the mast, but seeme
```

`memory_meta` check:

```text
[('embedding_dimension', '3072'), ('embedding_model', 'text-embedding-3-large')]
```

### 5.4. Notes / deviations

- `skipped_existing` is implemented as "existing ids observed before upsert" rather than
  suppressing embedding. This keeps the required second-run replay-cache proof
  (`cache_hits=89`, incremental cost 0) while Chroma idempotency is still guaranteed by fixed
  IDs and `upsert`.
- `query_similar`, `query_motifs`, and `query_tm` require an `EmbeddingClient` argument to avoid
  Chroma's default embedding function and keep tests/network behavior explicit.
- Real pilot token usage was 6,243 embedding tokens, lower than the rough task estimate.

## 6. Review *(Claude điền — 2026-06-13)*

- **Verdict: PASS.** Tầng vector của P4 sẵn sàng cho Context Builder (P4-02).
- Tự kiểm chứng (theo quy tắc tái tính độc lập): 68/68 tests tự chạy; tự chạy lại CLI
  build thật → 81 passages / 8 motifs / 0 TM, **cache_hits 89, cost $0** (tái lập
  hoàn hảo); tự query memory_meta → `embedding_model=text-embedding-3-large`,
  `dimension=3072` đúng §4.2; key fallback `API-KEY.txt` không log key.
- Đối chiếu spec: 3 collections đúng §4.1 (cosine, metadata đủ {doc_id, chapter_id,
  block_id, kind}); TM tạo rỗng + `add_tm_entry` embed vế EN làm khóa, VI = payload,
  docstring ghi rõ chỉ caller pass-Critic được add; `query_tm` filter
  `chapter_id $in chapter_window`; model/dim mismatch → raise re-index; idempotent
  qua upsert (lần 2: existing_ids 89, số vector không đổi); cache per-text đúng
  thiết kế (đổi batch không vỡ cache).
- Smoke query chất lượng tốt: "the old captain at the inn" → top-5 toàn block
  captain/quán trọ (top-1 = heading "At the Admiral Benbow"), hai lần chạy kết quả
  giống hệt từng chữ số distance — bằng chứng determinism trình diễn được.
- Chi phí thật: $0.000812 / 6.243 tokens — rẻ hơn ước tính 10×.
- Deviation khai đúng quy trình: `chromadb>=1,<2` pin theo bản cài 1.5.9 (§5).
- Findings nhỏ (không chặn): (1) query helpers nhận `embedding_client` làm tham số —
  lệch nhẹ signature phác trong spec nhưng hợp lý hơn (tái dùng cache khi query);
  (2) smoke query embedding cũng đi qua cache nên lần 2 free — tốt.
- Follow-up: không có. P4-02 (Context Builder 4 tầng + hard constraints → S1) là
  task kế tiếp.
