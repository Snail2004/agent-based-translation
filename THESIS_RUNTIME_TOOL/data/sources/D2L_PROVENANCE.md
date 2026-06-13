# D2L source provenance (raw clone gitignored)

- remote: https://github.com/d2l-ai/d2l-vi.git
- branch: main
- commit: c775d6b4998e6243ec5d11f950e67679555a2c74
- cloned_size: ~214 MB (84 MB .git)
- layout: `*_origin.md` = EN gốc D2L (chuẩn); `index.md`/`<section>.md` = VI; `glossary.md` = bảng EN→VI người-chuẩn (aivivn) = GOLD cho TAR

## CẢNH BÁO chất lượng (Claude, 2026-06-13)
Bản VI repo này là MÁY DỊCH THÔ chưa hiệu đính → KHÔNG dùng làm tham chiếu BLEU/COMET.
Bằng chứng: "neural network" glossary chốt `mạng nơ-ron` nhưng MT dùng `mạng thần kinh`
(sai) ở 57 file vs `mạng nơ-ron` ở 17 file; tiếng Anh xen giữa câu. → D2L chạy TAR (vs
glossary gold) + self-consistency + judge; reference-metrics chờ bản người aivivn/d2l-vn
(EV-03). Xem LOCK (cc).

## Tái tạo
git clone https://github.com/d2l-ai/d2l-vi.git && git -C d2l-vi checkout c775d6b
