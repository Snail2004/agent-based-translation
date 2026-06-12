# TASK_P4_01_embeddings_chroma — Embedding client + dựng 3 Chroma collections từ memory frozen

- **Status:** READY
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

—

## 6. Review *(Claude điền)*

—
