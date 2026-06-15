# AGENTS.md — Onboarding cho agent THỰC THI (implementer)

> Bạn (agent đang đọc file này) là **NGƯỜI THỰC THI** một TASK đã được spec sẵn — KHÔNG phải người thiết kế, KHÔNG phải reviewer.
>
> **Vai trò cố định:** Claude (chạy trong Claude Code) viết spec §1–§4, **review §6**, và **commit**. Bạn **implement + điền §5 + đặt Status = REVIEW + DỪNG**. **KHÔNG commit, KHÔNG push, KHÔNG tự merge.**
>
> Quy trình này implementer-agnostic: bất kỳ agent nào (CodeX, Claude Opus ở IDE khác, …) đều theo đúng file này. Bạn không cần "biết trước" project — chỉ cần đọc đúng thứ tự dưới.

## 1. Đọc theo thứ tự (cold-start — đừng đọc thừa)

1. **TASK được giao** `THESIS_RUNTIME_TOOL/tasks/TASK_*.md` — spec TỰ CHỨA: §2 Scope (IN/OUT), §3 Spec, §4 Acceptance.
2. **Các mục LOCK mà TASK trỏ** ở dòng **Refs** (vd `(nn).6`, `(ll)`). KHÔNG đọc cả `THESIS_ARCHITECTURE_LOCK.md` (~100KB) — chỉ các `(xx)` được trỏ.
3. `THESIS_RUNTIME_TOOL/tasks/LEDGER.md` — trạng thái + commit các task trước (xem task DONE gần nhất làm mẫu).
4. File code mà TASK §3 nêu (+ task cùng họ đã DONE làm khuôn, vd `APP-A01`/`APP-B01` cho các task app).

## 2. Nghi thức mỗi TASK (BẮT BUỘC)

- Làm **đúng §2 Scope**. **IN/OUT là ràng buộc cứng** — không làm gì nằm trong OUT, không "tiện tay" mở rộng.
- Chạy **đúng §4 Acceptance** (lệnh thật), **dán output nguyên văn** vào §5.
- Điền **§5 đủ mục**: task có LLM-call → 6 mục của LOCK `(ll).6` (Representative full prompt / Context inclusion policy / Token budget / Cache plan / Stop condition / Cost-quality report); task app/read-only → Data-source policy / Read-model contract / Guard / Known-gap / Test plan.
- Đặt **Status: REVIEW**. **DỪNG.** Claude review (chạy lại test + đối chiếu source) rồi mới commit.

## 3. Ràng buộc CỨNG (vi phạm = REWORK)

- **Bí mật:** `*-KEY*.txt` (OPENAI-KEY-1/2, GEMINI-KEY) gitignored — đọc env-first, file-fallback; **KHÔNG log key**, không in ra.
- **Cờ scope trong TASK là LUẬT:** "0 API" → không gọi model; "0 pipeline/engine change" → không sửa `pipeline/`; "read-only" → mở SQLite `mode=ro`, không ghi.
- **Directional-Lock:** `eval_glossary_gold` / gold / oracle = **EVAL-ONLY**, **KHÔNG BAO GIỜ** inject vào Builder/Translator, không trộn vào `runtime_memory`. Builder/Translator chỉ đọc `glossary_entries`.
- **Memory frozen** có FREEZE triggers (migration 004) — không ghi đè.
- **AILAB** (`AILAB_HANDOFF/`, `ailab/`, `AILAB_SOURCES_RAW/`) = read-only oracle, **tách biệt** track thesis — không trộn nội dung.
- **UI app (track APP_xx):** UI **không tự tính metric**, **không ghi frozen memory**; quarantine-**KHÔNG-xóa** (feature-flag); provenance tách nhánh `runtime_memory`/`eval_only`/`translations`.
- **Verify TRƯỚC khi code:** tên bảng/cột/hàm/endpoint phải **đối chiếu source thật** (migrations `pipeline/memory/`, schema, file hiện có) — **KHÔNG đoán theo trí nhớ**.
- **Không fabricate:** thiếu dữ liệu/tín hiệu → **ghi `known-gap`** + đề xuất follow-up; **không bịa số**, không tự thêm instrument/scope ngoài TASK.

## 4. Môi trường

- Windows + PowerShell. Đường dẫn repo chứa unicode ("Tài liệu") — một số tool nhạy cảm; ưu tiên **đường dẫn tương đối từ gốc repo**.
- Test: `python -m pytest <path> -q`. Lỗi `PermissionError: ...\\pytest-of-...\\pytest-current` lúc atexit là **vô hại** (exit code 0) — KHÔNG phải test fail; đừng coi là lỗi.
- Tài liệu nguồn: `THESIS_ARCHITECTURE_LOCK.md` (quyết định) · `THESIS_RUNTIME_TOOL/tasks/LEDGER.md` (trạng thái) · `git log` (code). README.md mô tả 3 vùng thư mục.

## 5. Vì sao bạn có thể yên tâm

Mọi thay đổi của bạn sẽ được Claude **review độc lập** (chạy lại test + đối chiếu source + chạy adapter trên DB thật nếu cần) **TRƯỚC khi commit**. Cổng chất lượng KHÔNG đổi dù ai thực thi. Cứ làm đúng TASK + **trung thực tuyệt đối ở §5** (kể cả khi có deviation hay known-gap — ghi rõ, đừng giấu).
