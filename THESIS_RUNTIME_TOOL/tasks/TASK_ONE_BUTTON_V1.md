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

## 2b. Q1-Q3 CHOT (user, 2026-07-05)

- **Q1 = TUY CHON (user de xuat, hay hon 2 phuong an goc):** default [DICH] chi chay S1 + thuoc reference-free; UI co checkbox "Chay kem S0 de so sanh (benchmark, ~x2 chi phi)" — bat len thi bao cao co them cot S0 + PJ so-cap. Mode bao cao van tach biet voi toggle nay (Q3).
- **Q2 = CHAY NEN + BAO CAO 2 DOT** (dung de xuat): progress tung stage; dot 1 = ban dich + TC/TA + hygiene; dot 2 = cascade/SF-QE/SF-BT (+PJ neu S0 bat) tu bo sung.
- **Q3 = MOT RENDERER NHAN THAM SO MODE** (dung de xuat).
- **Dieu kien user them:** thiet ke day du phai duoc trinh bay va CHOT truoc khi code — khong vua lam vua sua. Design review se ghi thanh §2c sau khi user phan hoi y tuong.

## 2c. DESIGN CHOT — ba mat tien, mot xuong song (user + Claude, 2026-07-05)

**Y tuong user (2 diem, nhan ca 2):** (1) BO stream ban dich thoi gian thuc — nang, marks/overlay can block hoan chinh + cascade xong; trang Chapter/Preview = mat tien REVIEW ket qua cuoi; (2) THEM man hinh Agent Console kieu ainovel-cli (repo tham chieu da co trong memory): stream tac vu song — agent nao dang chay, goi tool/LLM gi, toi dau, cost/cache — thay cho Cockpit tinh.

**Kien truc:** [DICH] -> Panel xac nhan (uoc tinh $ tung stage, tran budget, checkbox S0 benchmark ~x2) -> **AGENT CONSOLE** (view MOI, live) -> Chapter/Report (review khi xong tung dot).
- Console layout: trai = tong quan run (stage x/8, cost luy ke vs tran, cache hit %, health LM Studio/API); giua = event stream dong thoi gian (agent + model + window/block + tokens + $ + cache hit/miss) + preview block vua dich xong (block-level tick, KHONG token-stream); phai = checklist 8 stage + link artifact hien dan + watchlist §36 cho duyet.
- Cockpit hien tai GIU NGUYEN lam view hau kiem (prompt/cache forensics) — khong nhap voi Console: mot cai "DANG xay ra gi", mot cai "DA xay ra gi va vi sao".
- **Xuong song: event bus JSONL per-run** — schema thong nhat (ts, run_id, stage, agent, event_type, payload{tokens, cost, cache_hit, block_id}); moi stage CLI phat vao mot file; UI tail. Nen mong DA CO MOT NUA: Cockpit da co sidecar-events + live-log-tail panel, run_*_events.jsonl da ton tai — viec con lai la chuan hoa schema + phu day du stage + view moi. Event log nay DONG THOI la ho so audit/tai lap (mot cong doi viec, dung ky luat manifest).
- Chi tiet cap implement (SSE vs polling, schema field chinh xac) = CodeX de xuat trong khuon nay, Claude review.

**Demo hoi dong:** man hinh Console khi dang chay = bang chung song "he da-agent dang lam viec"; Chapter + Report khi xong = san pham. Trinh tu thi cong giu nguyen §4 (agreement analysis truoc).

## 2d. EVENT SCHEMA v1 KHOA — hop dong 3 ben Console/Orchestrator/CLI (Claude adjudicate 10 diem CodeX vong 1, hoi tu khong can vong 2, 2026-07-05)

**Path (CodeX #1 — theo quy uoc CO SAN cua backend):** `run_events/<run_id>.jsonl`, append-only, doc bang byte-offset nhu Cockpit hien tai. CAM de quy uoc song song.

**Envelope moi event:**
`{v:1, event_id:"<run_id>.<attempt_id>.<seq>", run_id, attempt_id:int, seq:int (monotonic PER attempt), resume_from:{attempt,seq}|null, ts:ISO, stage:1-of-8 (builder|auditor|translator|cascade|sf_qe|sf_bt|pj|report), script:"ten module that (run_translate|score_sf_bt|...)", agent:vai-tro-on-dinh (Builder|Auditor|Translator|Localizer|Evaluator|Reporter|Orchestrator), event:type, severity:info|warning|error, payload:{...}}`

**Event types (lifecycle du — CodeX #8):** run_start|run_done|run_failed|run_cancelled|run_resumed|heartbeat|health_check|stage_start|stage_done|stage_skipped|checkpoint|llm_call|tool_call|block_done|artifact_created|retry|gate_pause|cost_snapshot|warning|error.

**Payload fields (optional theo type):** model, provider, prompt_version, prompt_sha, cache_key, cache_hit, provider_cached_tokens, tokens_in, tokens_out, **cost_delta_usd** (CLI chi phat delta), duration_ms, block_id, unit(window|block|term|occurrence|call|chapter|artifact), scope{chapter_id, config}, progress{done, total, total_known:bool}, artifact_path, artifact_type, artifact_sha, error_code, retry_count, budget_cap_usd, message(<=200 chars).

**Luat cost (chinh cua Claude tren #4 de tranh race da-process):** stage CLI CHI phat cost_delta_usd; UI tu cong don tu stream; orchestrator (single writer cap run) phat `cost_snapshot` {cost_total_usd, tokens_total, budget_cap_usd} CO THAM QUYEN tai moi ranh gioi stage — lech giua 3 nguon = tin hieu debug, khong phai lay nguon nao ghi de nguon nao.

**Luat emitter (CodeX #5):** mot emitter chung `pipeline/lib/events.py`, buffer + flush theo nhip (moi 20 event hoac 0.5-1s), FORCE flush tai run_*/stage_*/gate_pause/warning/error/cost_snapshot. **KHAC LUAT: event log = observability + audit; RESUME doc manifest/artifact, KHONG BAO GIO doc event log** — cam bien no thanh state machine.

**Luat poll (CodeX #6):** byte-offset + max_bytes/poll (256KB) + `truncated:true` khi cat; orchestrator heartbeat 30s; UI stalled-badge khi >90s khong event/heartbeat.

**Luat rieng tu/nhe (CodeX #9):** CAM dump full prompt/output vao event; chi prompt_sha/prompt_version/cache_key/artifact_path — full prompt doc o Cockpit Inspector (da co). Giam nang UI + chong lo key/context.

**Fixture replay (CodeX #10):** CA HAI — convert log that run_preliminaries_events.jsonl sang schema v1 (regression compatibility) + synthetic ~500 event phu du 20 type ke ca gate_pause/error/retry/health_check/stage_skipped ma log cu chua co.

**Trinh tu UI-1 giu nhu da ban:** emitter lib -> fixture kep -> trang Agent Console (route rieng; header run/cost/cache; feed filter theo stage/agent/severity; checklist 8 stage + progress + artifact link) chay che do replay x10 -> STOP screenshot cho Claude/user review; chua cam pipeline that.
