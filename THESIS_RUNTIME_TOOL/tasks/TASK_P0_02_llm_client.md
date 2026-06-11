# TASK_P0_02_llm_client — LLM client (pin model, seed, reasoning_effort) + replay cache + quota tracking

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §2.2 (model stack), §5.1 (replay cache, reasoning tokens), §2.1 (failure policy — phần của Coordinator, KHÔNG nằm trong client này)
- **Branch/Commit:** (điền khi imple xong; commit `P0-02: ...` trên nhánh `main`)

## 1. Bối cảnh & mục tiêu

Mảnh cuối của Phase P0. Mọi LLM call của pipeline (World Builder, Narrative, Translator,
Critic T2) sẽ đi qua MỘT client duy nhất để bảo đảm: model pin cứng, tham số tái lập
(temperature/seed) được ghi lại, reasoning tokens không âm thầm đốt tiền, mọi call
replay được khi chạy lại/resume, và quota free 2,5M tokens/ngày được theo dõi.
KHÔNG có logic dịch/prompt nào trong task này — client là hạ tầng thuần.

## 2. Scope

**IN:**
1. `pipeline/agents/llm_client.py` — class `LLMClient` (OpenAI, Chat Completions API).
2. `pipeline/agents/llm_config.py` — dataclass `LLMConfig` + loader YAML.
3. `pipeline/configs/llm_default.yaml` — config mẫu (model pin, temperature, seed,
   reasoning_effort, verbosity, max_output_tokens, daily_token_cap, pricing).
4. Replay cache (sqlite, file riêng do caller chỉ định — KHÔNG ghi vào memory.sqlite3).
5. Usage/quota tracking theo ngày (cùng file cache db).
6. `pipeline/requirements.txt` (openai, pyyaml, pytest).
7. Tests offline 100% (`pipeline/tests/test_llm_client.py`) — KHÔNG gọi mạng.
8. `pipeline/scripts/llm_smoke.py` — smoke thủ công 1 call thật khi có
   `OPENAI_API_KEY` (chạy tay, KHÔNG nằm trong pytest).

**OUT:** Gemini judge client (task pha eval); embedding client (task P2); logic re-ask
khi JSON sai schema (việc của Coordinator, §2.1); prompt assembly/zones (P4);
mọi agent prompt. KHÔNG đụng `app/`.

## 3. Spec

### 3.1. LLMConfig (YAML → dataclass)

```yaml
# pipeline/configs/llm_default.yaml
model: "gpt-5.4-mini"          # PIN — cấm alias latest/chat-latest
temperature: 0.3
seed: 20260612
reasoning_effort: "minimal"     # LOCK §5.1: dịch không phải bài toán logic
verbosity: "low"                # nếu SDK/model hỗ trợ; không thì bỏ qua, ghi chú §5
max_output_tokens: 2048
daily_token_cap: 2400000        # chừa lề dưới quota 2.5M/ngày
pricing:                        # USD per 1M tokens — để ước tính cost
  input: 0.25
  cached_input: 0.025
  output: 2.00
```

### 3.2. LLMClient

- `LLMClient(config: LLMConfig, cache_path: str|Path, transport=None)`
  - `transport`: callable injectable cho test (mặc định = OpenAI SDK thật).
    Test KHÔNG BAO GIỜ chạm mạng — mọi test dùng fake transport.
- `call(messages: list[dict], *, response_format: dict|None = None, tag: str = "") -> LLMResult`
  - `LLMResult`: `text`, `parsed_json|None` (nếu response_format json và parse được;
    parse fail → `parsed_json=None` + `json_error` — KHÔNG tự re-ask),
    `model`, `system_fingerprint`, `usage` (prompt/cached/completion/reasoning tokens),
    `cost_usd`, `latency_ms`, `from_cache: bool`, `cache_key`.
- API: Chat Completions (`client.chat.completions.create`) với `model`, `messages`,
  `temperature`, `seed`, `reasoning_effort`, `response_format`, `max_completion_tokens`.
  Đọc về: `choices[0].message.content`, `system_fingerprint`,
  `usage.prompt_tokens_details.cached_tokens`, `usage.completion_tokens_details.reasoning_tokens`.
  > Nếu SDK hiện tại lệch tên field/param: CodeX tự xác minh với SDK đang cài, làm
  > theo SDK thật và GHI RÕ deviation vào §5 — đừng làm theo spec mù quáng.
- CẤM trong client: `tools` param (deterministic context feeding), streaming, log API key.

### 3.3. Replay cache (chốt theo LOCK §5.1)

- `cache_key = sha256(canonical_json({model, messages, temperature, seed, reasoning_effort, response_format}))`
  — canonical = json.dumps(sort_keys=True, ensure_ascii=False).
- Bảng (trong file cache db riêng, `CREATE TABLE IF NOT EXISTS`):
  `llm_call_cache(cache_key TEXT PK, model TEXT, tag TEXT, request_json TEXT,
  response_text TEXT, system_fingerprint TEXT, usage_json TEXT, cost_usd REAL,
  latency_ms INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)`.
- Cache HIT → trả LLMResult từ cache (`from_cache=True`), KHÔNG gọi transport,
  KHÔNG cộng quota.
- Tham số `bypass_cache: bool = False` cho trường hợp cần ép gọi lại.

### 3.4. Retry & quota

- Retry: lỗi 429/5xx/timeout → exponential backoff (1s, 2s, 4s… + jitter), tôn trọng
  `Retry-After` nếu có, tối đa `max_retries=5` → raise `LLMTransportError`.
  Lỗi 4xx khác (400/401/404) → raise ngay, KHÔNG retry.
- Quota: bảng `usage_daily(date TEXT PK, total_tokens INTEGER, calls INTEGER)` trong
  cache db; mỗi call thật cộng `prompt+completion tokens`. `get_usage_today()` public.
  Nếu `total_tokens + ước_lượng > daily_token_cap` → raise `DailyQuotaExceeded`
  (Coordinator sẽ checkpoint/resume hôm sau — ngoài scope task này).

## 4. Acceptance criteria (lệnh chạy được, offline 100%)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL
python -m pytest pipeline/tests/test_llm_client.py -v
# PHẢI PASS (CodeX viết, dùng fake transport, không mạng):
# 1. test_cache_hit_skips_transport: call 2 lần cùng request → transport chỉ bị gọi 1 lần,
#    lần 2 from_cache=True, kết quả giống hệt, quota không tăng
# 2. test_cache_key_sensitivity: đổi seed / model / 1 ký tự message / response_format
#    → cache_key khác nhau (4 biến thể)
# 3. test_retry_backoff_429: transport ném 429 hai lần rồi thành công → call thành công,
#    đếm đúng 3 lần gọi transport (sleep được monkeypatch để test nhanh)
# 4. test_no_retry_on_400: transport ném 400 → raise ngay, transport bị gọi đúng 1 lần
# 5. test_usage_and_quota: 2 call thật (fake) → usage_daily cộng đúng; set cap nhỏ
#    → call thứ 3 raise DailyQuotaExceeded; cache hit không cộng quota
# 6. test_json_mode: fake transport trả JSON hợp lệ → parsed_json đúng; trả JSON hỏng
#    → parsed_json=None + json_error, KHÔNG raise, KHÔNG retry
# 7. test_cost_estimate: usage giả định → cost_usd đúng theo pricing trong config
#    (tính cả cached_input giá rẻ)

python -m pytest pipeline/tests/ -v   # toàn bộ pipeline tests (gồm migration cũ) vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

—

## 6. Review *(Claude điền)*

—
