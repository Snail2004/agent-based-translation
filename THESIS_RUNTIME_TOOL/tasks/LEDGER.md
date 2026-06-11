# THESIS WORK LEDGER — sổ truy vết công việc

> **Cold-start cho agent mới (đọc theo thứ tự, KHÔNG cần đọc gì khác):**
> 1. `../../THESIS_ARCHITECTURE_LOCK.md` — mọi quyết định kiến trúc + changelog.
> 2. File này — trạng thái mọi việc, commit tương ứng.
> 3. Mở đúng `TASK_*.md` đang liên quan.

## Quy ước (chốt 2026-06-11, LOCK §8.2)

- **1 việc = 1 file** `TASK_<Px>_<nn>_<slug>.md` theo `TASK_TEMPLATE.md`. Spec + imple
  + review sống chung 1 file. Vòng đời: `READY → IMPLEMENTING → REVIEW → DONE / REWORK`.
- **Phân vai:** Claude viết §1–§4 (spec + acceptance) → user đưa CodeX imple, CodeX điền
  §5 (implementation notes + test output) → Claude review điền §6 (verdict) → cập nhật
  bảng dưới.
- **Acceptance criteria (§4) phải là lệnh chạy được** (pytest/script), không phải mô tả.
- **Commit:** `P0-01: tóm tắt` (1 task = 1 commit chính); tài liệu → `docs:`; đồ clone
  → `vendor:`. Tag mốc phase: `P0-done` … `P5-pilot`. Nhánh dài hạn: `thesis/main`.
- **Quyết định kiến trúc thay đổi** → ghi LOCK §10 changelog, KHÔNG ghi ở đây.
- **Gotcha kỹ thuật** → §5 của task; ảnh hưởng dài hạn → thêm cột Ghi chú + (nếu là
  quyết định) LOCK changelog.
- Tasks AI-LAB cũ (clone theo) nằm ở `_ailab_legacy/` — chỉ tham khảo, không làm theo.

## Bảng trạng thái

| Task | Tóm tắt | Status | Commit | Review | Ghi chú |
|---|---|---|---|---|---|
| P0-01 | Vệ sinh clone + skeleton pipeline/ + migration schema v3 | DONE | `P0-01:` (xem git log) | PASS (Claude, 2026-06-12) | 4/4 migration + 88/88 smoke; DDL khớp spec |
| P0-02 | LLM client: pin model + seed + reasoning_effort + replay cache + quota | READY | — | — | Spec: TASK_P0_02_llm_client.md; tests offline 100% |
