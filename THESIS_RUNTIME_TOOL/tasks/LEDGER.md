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
| P0-02 | LLM client: pin model + seed + reasoning_effort + replay cache + quota | DONE | `P0-02:` (xem git log) | PASS (Claude, 2026-06-12) | 11/11 tests; chặn alias latest từ config; **P0 hoàn thành** |
| P1-01 | Nguồn TI sạch (strip annotations) + loader document.json → blocks | DONE | `P1-01:` (xem git log) | PASS (Claude, 2026-06-12) | 23/23 tests; source 40ch/1476 blocks tracked + PROVENANCE sha256; chốt chặn Directional Lock (raise nếu còn annotations) hoạt động; trục block_id thesis↔oracle pinned |
| EV-01 | Module TAR/ECS/FVR + đo lần đầu trên oracle output | DONE | `EV-01:` (xem git log) | PASS (Claude, 2026-06-12) | 23/23 tests; **số đầu tiên: TAR 0.8866 / FVR 0.0 / ECS 0.9195**; 3 caveat đọc số ghi ở §6 task (Jim=artifact narrator, glossary termhood, occurrence provider) |
| P2-01 | World Builder pre-pass: trích T1–T4 từ text trắng, pilot ch02–ch03 TI | DONE | `P2-01:` (xem git log) | PASS (Claude, 2026-06-12) | 29/29 tests; **GO/NO-GO #1: GO** (json_fail 0.0); probe API → LOCK (v) (minimal không tồn tại, temp chỉ với effort=none; sửa llm_default); 6 findings phải vá ở P2-02 (§6 task — nặng nhất: prompt hardcode "Jim Hawkins" = tri thức ngoài văn bản) |
| P2-02 | Fix prompt + re-run pilot + Span Resolver + persist T1–T4 + FREEZE | DONE | `P2-02:` (xem git log) | PASS (Claude, 2026-06-12) | 37/37 tests; **bằng chứng tự học: ch02 narrator vô danh → ch03 học tên "Jim" từ text**; FREEZE probe DB thật OK (memory chặn, runtime ghi được); timeline xuyên chương OK; 2 zero-occurrence = apostrophe ’ vs ' (follow-up nhỏ task kế); **P2 pilot HOÀN THÀNH** → tag `P2-done` |
| P3-01 | S0 end-to-end: windower + dịch ch02+ch03 + đo S0-vs-oracle cùng thước | DONE | `P3-01:` (xem git log) | REWORK→PASS (Claude, 2026-06-13) | **Milestone "dịch được" GVHD ĐẠT**: 81/81 blocks, 31 windows, $0,021; **S0 TAR 0.4151 vs oracle 0.6226 cùng 53 pairs** (số đã tái tính độc lập); ECS 0.76≈0.77; mẫu đắt giá "ngực người chết" vs "rương người chết"; REWORK do agent ngoài bịa số/mẫu + giả danh reviewer — CodeX làm sạch; quy tắc mới: headline số phải được reviewer tái tính |
| P4-01 | Embedding client + dựng 3 Chroma collections từ memory frozen | DONE | `P4-01:` (xem git log) | PASS (Claude, 2026-06-13) | 68/68 tests (tự chạy); 81 passages / 8 motifs / 0 TM; rebuild $0 (89 cache hits, tự xác minh); memory_meta pin model+dim; smoke query "old captain at the inn" → top-5 đúng vùng, deterministic; cost thật $0.0008 |
