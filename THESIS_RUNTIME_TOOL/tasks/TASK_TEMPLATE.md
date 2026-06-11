# TASK_<Px>_<nn>_<slug> — <một dòng tóm tắt>

- **Status:** READY | IMPLEMENTING | REVIEW | DONE | REWORK
- **Refs:** THESIS_ARCHITECTURE_LOCK §… | RUN_EVAL_SCHEMA §… | PROMPT_DESIGN §…
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

Vì sao việc này tồn tại, nó phục vụ phase/quyết định nào trong LOCK.

## 2. Scope

- **IN:** …
- **OUT:** … (ghi rõ để CodeX không lan man)

## 3. Spec *(Claude viết)*

File sẽ đụng tới, interface/schema/hàm cụ thể, ràng buộc phải tuân (trích LOCK).

## 4. Acceptance criteria *(Claude viết — lệnh chạy được, không phải mô tả)*

```bash
# ví dụ:
# python -m pytest pipeline/tests/test_migration.py -v   → PASS toàn bộ
# python pipeline/scripts/check_schema_version.py        → in ra "3"
```

## 5. Implementation notes *(CodeX điền)*

- Đã làm gì, file nào đổi, quyết định nhỏ nào tự đưa ra (và vì sao).
- Output các lệnh acceptance (dán nguyên văn).
- Gotcha/quirk phát hiện trong lúc làm.

## 6. Review *(Claude điền)*

- **Verdict:** PASS / REWORK (lý do)
- Findings: …
- Follow-up (nếu có): tạo TASK mới, không nhét thêm vào task này.
