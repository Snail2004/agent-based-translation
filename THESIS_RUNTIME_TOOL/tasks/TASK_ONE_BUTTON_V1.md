# TASK ONE-BUTTON V1 — [DỊCH] -> bản dịch + báo cáo đầy đủ, tự động end-to-end

**YÊU CẦU KHÓA (user, 2026-07-04):** sau khi full bộ chấm hoàn tất, toàn bộ hệ chấm điểm PHẢI tích hợp vào app thành MỘT flow tự động Builder -> Translator -> chấm -> báo cáo. Người dùng bấm [DỊCH] là nhận bản dịch kèm báo cáo, không thao tác tay giữa chừng. (Yêu cầu gốc của Thầy; khung §36.4 TASK_BUILDER_V2 + chế độ báo cáo §5d TASK_EVAL_SCORING_V1.)

## 1. Chuỗi stage (mọi mảnh ĐÃ TỒN TẠI dạng CLI — cột phải là việc còn thiếu)

| # | Stage | Đã có | Còn thiếu cho one-button |
|---|---|---|---|
| 1 | Ingest tài liệu -> blocks DB | ✅ (job pipeline) | — |
| 2 | Builder v2 (C2->C3->§29->§30 pack) | ✅ | gọi chuỗi tự động + cost-gate hiển thị trong UI |
| 3 | §36 re-election + watchlist | ✅ probe standalone | cắm vào flow (post-Auditor), flip CHỈ áp sau human review |
| 4 | Translator S1 + hygiene | ✅ run_translate | — |
| 5 | Cascade localize (marks + occ + evidence) | ✅ run_experiment_cascade | bỏ ràng buộc experiment-id 2-arm; chạy 1-arm production |
| 6 | Chấm: TC/TC-Occ/TA-Occ + SF-QE + SF-BT + gates | ✅ score_run, score_sf_qe; SF-BT/PJ đang làm | orchestrator gọi tuần tự |
| 7 | Báo cáo tổng + watchlist + cặp câu bằng chứng | JSON rời rạc | RENDERER một bản báo cáo thống nhất (HTML/UI) |
| 8 | UI overlay + nút [DỊCH] | ✅ overlay/focus/manifest | nút + progress + trạng thái async |

## 2. Ba quyết định thiết kế PHẢI chốt trước khi code (đề xuất của Claude kèm theo)

- **Q1 — Production có chạy kèm S0 không?** Đề xuất: KHÔNG. S0 là công cụ thí nghiệm; production chỉ chạy S1 + các thước reference-free (TC-Occ vs từ điển tự xây, TA-Occ vs style guide user nếu nộp, SF-BT, SF-QE). Hệ quả: PJ dạng so-cặp KHÔNG có trong báo cáo production -> thay bằng PJ target-only (chấm trôi chảy đơn ngữ, đã ghi ở roadmap EVAL §6) hoặc bỏ khỏi production, chỉ dùng trong benchmark mode.
- **Q2 — Đồng bộ hay chạy nền?** Một chương = Builder (~phút, $) + Translator (~phút, $) + cascade (~1-2h local) + SF-QE (~20min CPU) + SF-BT (~giờ local). Đề xuất: bấm nút -> chạy NỀN có progress từng stage trong UI; báo cáo "nhanh" (TC/TA + hygiene) ra trước, báo cáo sâu (cascade marks, SF-*) bổ sung khi xong.
- **Q3 — Hai chế độ báo cáo (EVAL §5d):** benchmark mode (có gold: đủ 7 thang) vs production mode (không gold: TA-vs-gold ẩn, TA-Occ đổi nhãn "tuân thủ từ điển tự xây", + khe style-guide adapter §9). Renderer phải nhận mode làm tham số, không phải hai codebase.

## 3. Ràng buộc triển khai (ghi để không quên khi đóng gói)
- LM Studio + gemma-4-12b + bge-m3 là DEPENDENCY runtime của app (cascade T3, SF-BT, §36) — app phải kiểm tra endpoint trước khi chạy, báo lỗi tử tế.
- CometKiwi: py3.11 riêng (numpy<2), model gated HF (user máy mới phải tự login) — cô lập vào subprocess như score_sf_qe hiện tại.
- Mọi stage giữ nguyên kỷ luật: frozen ro, workdb copy, cost-gate hiển thị trần $ trước khi gọi API, log/manifest từng artifact.

## 4. Trình tự
1) Xong SF-BT + PJ + agreement (TASK_EVAL_SCORING_V1) — các thước phải chốt TRƯỚC khi đóng khung báo cáo.
2) Chốt Q1-Q3 với user.
3) Implement: orchestrator (một lệnh `run_one_button`) -> report renderer -> nút UI + progress.
4) Nghiệm thu: bấm [DỊCH] trên MỘT CHƯƠNG MỚI chưa từng chạy, không thao tác tay, ra bản dịch + báo cáo + overlay. Đó là demo bảo vệ.
