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

## 2e. RA SOAT BACKEND LAN 2 (Claude doc lai code that truoc UI-1, 2026-07-05) — 9 phat hien, CHO CodeX doi chieu

Nguon doc: app/backend/services/thesis_runs.py (RunControl + read_events), pipeline/translate/run_events.py (sink cu), app/prototype/index.html (stack = React 18 UMD + Babel standalone, khong build step). Cac "LUAT THEM" duoi day la de xuat bo sung hop dong §2d — chot chinh thuc sau adjudicate vong CodeX.

**F1 — HIGH, partial-line tail (bug se can Console live):** read_events seek(offset) roi doc theo dong, new_offset = tell(). Neu poll bat dung luc writer dang ghi do dang mot dong (mot flush toi 20 event co the bi cat giua chung), duoi dong bi bao parse_error VA offset nhay qua phan con lai -> event mat/vo. LUAT THEM: reader chi tieu thu den ky tu xuong dong CUOI CUNG trong vung doc; phan duoi de lai cho poll sau; new_offset = byte ngay sau dong hoan chinh cuoi. Ap dung ca khi cat theo max_bytes (cat giua dong -> lui ve ranh gioi dong).

**F2 — HIGH, hai process cung append mot file:** orchestrator (heartbeat 30s + cost_snapshot) ghi song song voi stage CLI dang chay. Python text-append co the tach mot flush thanh nhieu OS-write -> dong xen ke -> JSONL hong. LUAT THEM: emitter serialize moi flush thanh MOT buffer bytes (cac dong hoan chinh) va ghi bang MOT os.write tren fd mo binary append (hoac open-append-ghi-dong ngay tung flush); flush > 64KB thi tach theo ranh gioi dong. Giu tinh chat cua sink cu: emit KHONG BAO GIO raise (observability khong duoc lam fail run).

**F3 — MEDIUM, sink cu lech schema:** pipeline/translate/run_events.py phat schema "run_event_v1" (khong co v/stage/agent/severity/event_id; attempt_id la STRING mac dinh = run_id; seq tu 1). UI-1 khong dung sink nay; buoc noi pipeline that thay bang pipeline/lib/events.py — runner.py nhan sink object nen chi can doi class, khong doi call-site. Converter fixture (§2d #10) cover log cu.

**F4 — MEDIUM, replay phai di qua endpoint that:** read_events doi run ton tai trong registry + event_log_path phai nam trong jobs_root/run_events (path-check cung). Fixture replay vi vay KHONG the la file tinh do frontend tu doc: can REPLAY DRIVER (script dev tao entry registry + append dong fixture vao run_events/replay_<name>.jsonl theo toc do x10). Duoc cai hay hon mock: test dung path poll that end-to-end.

**F5 — MEDIUM, duong du lieu preview block chua dac ta:** §2c hua "preview block vua dich xong" nhung §2d cam nhet ban dich vao event (dung luat). LUAT THEM: block_done chi mang block_id; UI fetch text qua endpoint read-only co san (translation_preview — kiem pham vi o buoc noi pipeline that); replay mode khong co DB sau lung -> preview pane hien block_id + tick + nhan "(replay: khong co text)".

**F6 — MEDIUM, cong cost-gate chua phu one-button:** ALLOWLIST RunControl chua co run_one_button/cascade/score_sf_*/pj; confirm_token chi cap duoc qua prompt-preview cua run_translate. Panel xac nhan can uoc tinh TOAN chuoi stage -> orchestrator can mode --estimate-only phat token (tai dung co che PreviewToken/argv-digest co san). Ghi vao danh sach no-chan-duong; KHONG thuoc UI-1.

**F7 — LOW, event_id khong parse duoc:** regex run_id cho phep dau cham -> "<run_id>.<attempt>.<seq>" khong split nguoc duoc. LUAT: event_id la khoa dedupe OPAQUE, cam parse; run_id/attempt_id/seq da la field rieng trong envelope.

**F8 — LOW, severity trung lap voi event type:** warning/error vua la type vua la severity. LUAT: severity la truc loc chinh cua UI; event type warning/error BAT BUOC severity trung ten; type khac mac dinh info.

**F9 — LOW nhung bug that khi resume:** spawn_run mo stdout log che do "w"; resume tai dung run_id -> log attempt truoc bi XOA TRANG. LUAT: stdout log per-attempt (run_logs/<run_id>.a<attempt>.log) hoac mo "a" + marker attempt. (Event log khong dinh vi da append-only.)

**Khoang trong implementation THUOC UI-1 scope (giao CodeX cung dot):** read_events chua co max_bytes 256KB + truncated flag (§2d doi) + F1 partial-line + F4 replay driver + F8 severity rule trong emitter.

## 2f. CHOT sau vong CodeX doi chieu §2e — sua doi hop dong §2d (Claude adjudicate, 2026-07-05)

CodeX xac nhan F1, F3-F9; phan bien F2 dung cho: "mot os.write append" giam rui ro nhung KHONG phai bao dam hop dong. Claude GHI NHAN va chot nguyen tac CodeX de xuat, voi phuong an cu the do Claude chon:

**F2 CHOT — SINGLE-WRITER INVARIANT (thay the luat os.write o §2e):** moi file event log co DUNG MOT writer tai moi thoi diem.
- Che do one-button: stage CLI ghi vao file RIENG `run_events/<run_id>.stage_<stage>.a<attempt>.jsonl`; orchestrator la writer DUY NHAT cua file merged `run_events/<run_id>.jsonl` — tail cac file stage bang byte-offset (dung lai cung luat partial-line F1) va append vao file merged, kem event rieng cua no (heartbeat/cost_snapshot/stage_*).
- seq trong file merged do orchestrator (writer cua file do) cap — monotonic tam thuong vi single-writer; seq goc cua stage giu lai o payload.src_seq de truy vet. event_id van la khoa opaque cap theo writer cua file.
- Che do standalone (Cockpit chay le mot script, khong orchestrator): stage CLI ghi thang `run_events/<run_id>.jsonl` nhu hien tai — van dung mot writer, invariant giu nguyen.
- KHONG dung msvcrt.locking (them co che moi phai test), KHONG pipe stdout (spawn_run da chiem stdout cho log). Luat "mot buffer/mot os.write moi flush + emit khong bao gio raise" van GIU nhung ha cap thanh ky luat emitter, khong phai bao dam concurrency.

**Bo sung CodeX duoc GHI NHAN het:**
1. Replay driver CHI lay path tu `_event_log_path()`/event_root — cam nhan path tu do (chan ca van de Unicode normalization 'Tai lieu').
2. Registry them attempt first-class khi lam orchestrator: attempt_index, resumed_from, log_path PER ATTEMPT (fix F9), event_log_path per run.
3. Frontend: CAM `.map()` toan feed. Ring buffer raw store + render latest N (mac dinh 200) + counters tong hop theo stage/severity + filter truoc khi render. Chua can thu vien virtualization.
4. max_bytes + partial-line la MOT cap khong tach roi: cat 256KB xong PHAI lui ve newline hoan chinh cuoi truoc khi parse va truoc khi advance offset.
5. Replay test 3 lop: fixture file -> registry entry -> /events?offset endpoint -> UI. Cam mock truc tiep state React.

**Trang thai hop dong: §2d + §2e(F1,F3-F9) + §2f(F2 single-writer + 5 bo sung) = KHOA. Het vong review, sang UI-1.**

## 2g. UI-1 NGHIEM THU (Claude verify doc lap 2 vong, 2026-07-05) — DAT, commit cung muc nay

**Pham vi giao (§2f) da giao du:** pipeline/lib/events.py (emitter v1, mot buffer/mot os.write binary append, tach >64KB theo ranh gioi dong, emit khong raise, force-flush dung danh sach); read_events harden (max_bytes 256KB clamp 64B-1MB + chi parse den newline cuoi + khong advance offset qua dong do + truncated/partial_line flag); fixture kep (synthetic 500 event phu 20/20 type 8 stage + converted 640 event tu log that); replay driver qua registry that + path chi tu _event_log_path(); trang Agent Console (route console, KPI/checklist/feed filter/stalled badge/preview "(replay: no text)"); RunRegistry.refresh() cho cross-process.

**Vong 1 verify: loi phat hien bang so hoc.** KPI hien 423/500 event: 262144/309829 x 500 = 423.05 — vong poll chi tiep tuc khi result.running, run da done thi poll MOT lan roi bo duoi truncated (mat 77 event vinh vien du UI in chu "truncated poll"). Bug thu 2 cung ho: moi counter (cost/cache/warn-err/checklist) reduce tren ring buffer slice(-1000) — run that voi hang nghin llm_call se tran buffer va dem thieu cost, vi pham luat §2d "UI cong don tu delta".

**Vong 2 verify sau fix: DAT.** Drain: truncated -> re-poll 0ms, partial_line -> 600ms, doc lap voi running. Aggregate: updateRunEventAggregate cong don tang dan tai luc nhan batch (cost_total, cache hits/known, warn/err, per-stage count+progress max(), latest artifact/block, last_ts); ring buffer chi phuc vu render feed (latest 200). Claude tu chay: 29/29 test pass; endpoint tra 500/500 qua 2 poll; /api/version tra 0.6.0 + git_sha tu VERSION file; key-scan sach; frozen 64D989/workdb 922293 khong doi; khong dung sink cu, khong dung pipeline.

**Lech schema GHI NHAN (additive, khong pha hop dong):** envelope co them field `schema:"one_button_event_v1"` va `event_type` trung `event` (tien UI); event_id doi format `run:attempt:writer_id:seq` — HOP LE vi F7 tuyen bo opaque, con chong trung cross-writer tot hon format §2d goc. RunRegistry.refresh() doc lai ca file moi poll — chap nhan o quy mo hien tai, theo doi khi registry phinh.

**VERSIONING KHOA (tra loi cau hoi user):** file VERSION o root THESIS_RUNTIME_TOOL = nguon su that duy nhat, semver-lite v0.MINOR.PATCH — MINOR nhay moi buoc milestone da chot, PATCH cho fix trong buoc, v1.0.0 danh cho ban demo bao ve; backend /api/version doc VERSION + git short-sha; UI badge so UI-vs-API (lech = canh bao frontend cache cu); moi bump mot dong CHANGELOG.md. Hien tai 0.6.0 = UI-1. BAC BO kieu v1.0001 (khong phan biet feature/fix, khong truy vet commit).

**Con no truoc khi Console thanh live:** mot lo liveness da biet — neu MOT dong event > max_bytes cap 1MB thi reader khong bao gio advance (stall vinh vien); xac suat thap vi message cap 200 chars va cam dump prompt, ghi day de vong sau vá neu can. Buoc ke tiep theo trinh tu: no-chan-duong (F6 estimate-only token, run_translate frozen-db write-open, cascade 1-arm, §36 wiring, preflight health-check) -> orchestrator run_one_button -> renderer -> nut [DICH].

## 2h. DESIGN PASS CONSOLE — SKIN-ONLY (user chot 2 cau hoi, Claude tu implement, 2026-07-05)

**User chot:** (1) pham vi = Console full-screen TRUOC, app annotation giu nguyen, cai to tong the de sau one-button; (2) theme = CA HAI: paper+amber (kieu ainovel-cli) mac dinh + toggle dark terminal.

**Hop dong cung:** SKIN-ONLY — khong dung poll loop / aggregate / API / RunControl props; chi markup + CSS + conditional render sidebar. Chay song song an toan voi CodeX N1-N5 vi ranh gioi file roi nhau: Claude chi dung app/prototype/(app.jsx, parts_center.jsx, styles.css) + task file; CodeX dung pipeline/ + app/backend/.

**Thiet ke (theo ainovel-cli + layout §2c von da chot):**
- Vao tab Console: workspace an LeftSidebar + RightPanel (conditional render, khong unmount logic khac), console chiem full width.
- 3 cot: TRAI ~260px = tong quan run (status/stage/events/cost/cache/health, hang label-trai gia-tri-phai kieu TUI); GIUA = event stream (time HH:MM:SS + event type mau theo severity + stage-agent mo + message, filter giu nguyen) + preview strip; PHAI ~280px = checklist 8 stage + latest artifact + cho watchlist §36.
- Typography: monospace stack trong console; CSS variables --c-* rieng cho console, 2 bo gia tri theme-paper / theme-dark; toggle luu localStorage.
- Ly do bac full-overhaul: annotation dang chay tot, khong nam tren duong demo [DICH]->Console->Report; repo ainovel-cli la Go TUI khong co code web tai dung — screenshot + §2c la spec du.

## 2i. N1-N5 NGHIEM THU (Claude verify doc lap, 2026-07-05) — DAT + 1 fix cua Claude, v0.7.0

**Tu chay lai bang chung:** 438/438 test pass (159.9s); frozen 64D989/workdb 922293 nguyen ven; key-scan sach; smoke THAT preflight_check tren may nay.

**Doi chieu tung muc:**
- **N1 — ghi nhan TRUNG THUC:** rao chan --workdb (bat buoc non-preflight, cam trung path frozen, copy+purge) DA TON TAI o HEAD tu truoc — memory "latent" cua Claude da cu. Dong gop that cua N1 = refactor _open_readonly_db/_open_writable_workdb + test end-to-end khoa bang chung: ro connection tu choi CREATE TABLE, non-preflight mocked run -> source hash byte-identical + probe table CHI trong workdb + report ghi frozen before==after. DAT.
- **N2:** arm_mode single_arm|multi_arm trong base report + test khoa 1-arm khong bi coi la loi; regression 2-arm giu nguyen (bo test cu pass). DAT.
- **N3:** builder_v2_reelection --event-log/--run-id/--attempt-id, phat DUNG MOT gate_pause (stage=auditor, watchlist_only:true, artifact_path=watchlist.json, progress) qua pipeline/lib/events.py; test khang dinh preflight watchlist-only KHONG tao notebook_reelected.json. Flip van sau human review. DAT.
- **N4:** preflight_check CLI 5 check, JSON may-doc, khong bao gio in gia tri key (chi source name); emit 5 health_check event envelope v1 dung (severity warning khi fail). Smoke that: gemma + bge match (fuzzy normalize bat text-embedding-bge-m3), 2 key OK. **BUG Claude bat bang smoke that: timeout import 20s < cold-start torch -> cometkiwi false-FAIL voi CHINH py3.11 dung (import truc tiep OK, comet 2.2.7). Unit test mock subprocess nen khong thay — dung bai green-tests-khong-chung-minh-duong-that. Claude vá: timeout 180s; rerun smoke voi py311: 5/5 PASS, status=pass.** Ghi chu van hanh: orchestrator PHAI truyen --python <py311> cho check nay.
- **N5:** ALLOWLIST mo rong; API_CAPABLE them cascade/sf_bt/pj — gate KIN: scoring script khong co PREFLIGHT_ONLY flag -> allow_api=false bi chan dry_run_not_supported, allow_api=true doi token ma khong nguon nao cap (estimate-preview tu choi script ngoai 3 script co dry-run that) -> khong co duong API lau. Ghi nhan gioi han CodeX tu khai: estimate-preview chi phu run_translate/cascade/reelection; scoring can --estimate-only rieng truoc khi vao cong nay (viec cua buoc orchestrator). Ghi chu: build_argv nhanh script moi chua gan --event-log/--run-id (di qua extra_args) — orchestrator xu ly.

**VERSION 0.7.0** (milestone N1-N5) + CHANGELOG. Buoc ke: orchestrator run_one_button (single-writer merged event log §2f, tieu thu estimate-preview N5, goi preflight_check N4 voi py311, cascade 1-arm N2, watchlist gate N3).
