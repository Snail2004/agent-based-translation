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
- CometKiwi / `score_sf_qe`: py3.11 riêng (numpy<2), model gated HF (user máy mới phải tự login). `score_sf_qe.py` import `torch`/`comet` ở module-level nên cả script phải được orchestrator spawn bằng py3.11; không có lớp cô lập subprocess bên trong script.
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

## 2j. ORCHESTRATOR DESIGN — de xuat Claude, CHO CodeX phan bien 1 vong truoc khi code (2026-07-05)

Nen da khoa: 8 stage (§1), Q1-Q3 (§2b), event contract (§2d-§2f), Console (§2g-§2h), N1-N5 (§2i). §2j chi quyet nhung khoan con ho:

**J1 — Hinh dang:** `pipeline/scripts/run_one_button.py`, process cha duy nhat. Stage = subprocess `python -m pipeline.scripts.<script>` voi argv that (khong import-call) — ranh gioi trung voi RunControl, giu cach ly loi/env (py311 cho sf_qe). Args chinh: `--job-id --chapters --workdb --with-s0 (Q1 checkbox) --budget-cap-usd --resume <run_id> --event-log --run-id --attempt-id`.

**J2 — Manifest = nguon su that resume (KHAC LUAT §2d):** `data/jobs/<job_id>/one_button/<run_id>/manifest.json`, ghi atomic (tmp+os.replace) sau MOI chuyen trang thai stage. Fields: v, run_id, attempt, job_id, chapters, with_s0, workdb_path, budget_cap_usd, stages[8]{name, status(pending|running|done|failed|skipped), artifact_path, artifact_sha, exit_code, started/ended, cost_delta_cum}. **Resume granularity = STAGE**: --resume doc manifest, stage done + artifact sha khop -> skip; stage dang do/failed -> chay lai stage do TU DAU (khong mid-stage); attempt+1, log per-attempt (F9), event log per-attempt merge tiep.

**J3 — Event single-writer (thi cong §2f):** moi stage nhan `--event-log run_events/<run>.stage_<s>.a<n>.jsonl` rieng; orchestrator la writer DUY NHAT cua `run_events/<run_id>.jsonl`: tail cac file stage bang byte-offset (luat partial-line F1 dung read_events da harden — tai su dung ham, khong viet lai), append vao merged voi seq do orchestrator cap + payload.src_seq; xen event rieng: run_start/run_resumed, stage_start/done, heartbeat 30s, cost_snapshot tai ranh gioi stage, run_done/failed. Stage chua co emitter (score_run, sf_qe...) -> orchestrator TU phat stage_start/done thay mat (script khong can sua o buoc nay).

**J4 — Hai dot bao cao (Q2):** dot 1 = builder -> auditor -> [gate_pause watchlist N3, KHONG chan] -> translator -> score_run nhanh (TC/TA/hygiene) -> artifact_created bao cao dot 1. Dot 2 = cascade(1-arm N2, 2-arm neu with_s0) -> sf_qe(py311) -> sf_bt -> [pj CHI khi with_s0] -> report tong. Run chi run_done sau dot 2; dot 1 xong phat checkpoint {phase:1_done}.

**J5 — Cost gate:** vao cong = estimate-preview N5 (RunControl them run_one_button vao ALLOWLIST + ESTIMATE_PREVIEW; estimate_by_stage tong hop: builder/translator tu preflight co san, cascade/sf local = $0, PJ can `--estimate-only` MOI cho score_pj — viec trong scope buoc nay vi PJ la scoring API-capable duy nhat). Trong run: orchestrator kiem cost cong don tai moi ranh gioi stage, vuot budget_cap -> gate_pause + dung an toan (resume duoc). KHONG kiem giua stage (stage tu co cap rieng cua no).

**J6 — Loi & retry:** stage exit != 0 -> retry dung 1 lan (event retry) roi run_failed; khong retry stage API-ton-tien (translator/builder/pj) — fail thang, nguoi dung resume sau khi xu ly. Registry them attempt fields (§2f #2): attempt_index, resumed_from, log_path per attempt.

**Pham vi buoc nay KHONG gom:** renderer thong nhat (buoc sau), nut [DICH] UI (buoc sau), scoring --estimate-only ngoai score_pj, wiring emitter vao tung stage script (J3 cho phep orchestrator phat thay).

**Nghiem thu buoc nay:** chay run_one_button tren chuong DA CO cache (0-API hoac gan 0) -> manifest du 8 stage, merged event log hop le (validator: seq monotonic, event_id unique, parse 100%), Console hien thi live dung, resume giua chung hoat dong (kill giua stage -> resume -> done), frozen db nguyen ven.

## 2k. ORCHESTRATOR — adjudicate vong 1 + Claude review lan 2 tren code that (2026-07-05). CON 1 VONG CodeX nua truoc khi code (user yeu cau double-review vi day la logic run, khong phai UI)

**Adjudicate vong 1 CodeX — GHI NHAN CA 7:** (1) CAM truyen event args mu: orchestrator phan loai per-script capability (co emitter: run_translate/reelection/preflight_check; chua co: cascade/score_pj/score_run/sf_qe/sf_bt -> orchestrator phat stage_start/done thay); (2) tach loi doc JSONL thanh helper thuan `read_jsonl_events(path, offset, max_bytes)` — HTTP endpoint va orchestrator dung chung, KHONG tao registry gia cho stage file; (3) stage-resume v1 giu, manifest them `resume_policy: internal_window_resume` cho translator + bang chung windows_skipped/done trong report; (4) score_pj estimate tu so DA DO: judged = different_pairs − cache_hits (probe cache 0-API), calls = judged×2, token/call = p90 pilot, auto-tie = 0 call, cap = nominal × multiplier; (5) J4 phai co BANG stage→script→argv→artifact→cost-bearing; (6) Windows: cancel = kill process TREE (taskkill /T /F), heartbeat payload them active_child_pid/stage/last_stage_event_age_sec/child_returncode, manifest stage=running ma khong co process owner -> mark failed-resumable, retry ngan khi doc/hash workdb bi lock; (7) retry: cascade co GPT-fallback = COST-BEARING (khong auto-retry), config/preflight fail khong retry.

**Claude review lan 2 — kiem tren code that:**
- Translator internal resume XAC NHAN: runner.py docstring "windows where every block already has a draft run are skipped" + windows_skipped + event window_skipped. Diem (3) dung.
- cascade/score_pj arg surface XAC NHAN tran: khong --event-log/--run-id. score_pj co san --validate-only + --expected-db-sha256 — estimate-only nen xay tren duong validate (load pairs + auto-tie + probe cache, 0 API).
- **PHAT HIEN MOI: score_sf_qe.py import torch+comet o MODULE LEVEL -> ca script PHAI chay duoi py311 exe. Dong §3 file nay ("co lap vao subprocess nhu score_sf_qe hien tai") SAI so voi code — khong co subprocess isolation nao trong score_sf_qe. J4 table them cot INTERPRETER per stage; orchestrator spawn sf_qe bang py311 path (cung path truyen cho preflight_check --python).**
- Chuoi Builder cho chuong MOI chua khoa duoc tu phia Claude: LEDGER cho thay builder_v2_pilot.py = C2 online driver, builder_v2_c3_audit.py = C3; vi tri chay §29 re-election va §30 pack-gate trong chuoi production (script nao, artifact nao) can CodeX khai chinh xac — day la muc TRONG TAM vong 2.

**Giao CodeX vong 2 (van chi review/dac ta, chua code):** (a) dien BANG J4 day du 8 stage: stage | script | interpreter (py313/py311) | argv toi thieu cho chuong moi | input artifact | output artifact | cost-bearing y/n | emitter y/n — kem bang chung file-level, dac biet chuoi Builder C2→C3→§29→§30 va cho ro run_cascade_localize vs run_experiment_cascade vai tro gi; (b) chot chu ky `read_jsonl_events` helper + vi tri dat (app/backend/services? pipeline/lib?) sao cho ca Flask va orchestrator import duoc khong vong lap; (c) dac ta score_pj --estimate-only (tai dung validate path, cong thuc muc 4); (d) dac ta cancel/zombie tren Windows o muc pseudo-code (taskkill tree, manifest owner-pid rule); (e) xac nhan/bo sung pham vi KHONG-GOM. Sau vong 2: Claude review chot §2l roi moi phat prompt implement.

## 2l. ORCHESTRATOR SPEC KHOA (Claude chot sau 2 vong CodeX + 2 luot Claude doc lap; hoi tu, khong can vong 3; 2026-07-05)

**Claude tu kiem 5 claim nang nhat vong 2 — DUNG TUNG DONG:** sf_bt judge = gemini-2.5-flash + pricing args (COST-BEARING — J5 cu ghi "$0 local" la SAI, Claude nhan loi); score_run.py:111 + d2l_translate_score.py:75/87 hardcode ["S0","S1"] (S1-only se KeyError); run_translate --run-id/--attempt-id cung dest=attempt_id + EventSink schema cu; --memory-notebook co that; cascade production = run_experiment_cascade (run_cascade_localize = duong EV cu, CAM dung cho one-button).

**KHOA cac quyet dinh:**
1. **Bang J4 = bang CodeX vong 2** (builder=builder_v2_pilot py313 cost / auditor=builder_v2_c3_audit cost / substep c35_decollision / gate reelection --preflight-only emitter-v1 / translator=run_translate --configs+--memory-notebook / cascade=run_experiment_cascade 1-arm / sf_qe py311 / sf_bt py313 COST / pj py313 COST / report=score_run+renderer-sau). Cot interpreter bat buoc; sf_qe la stage py311 duy nhat.
2. **MO RONG SCOPE bat buoc cho Q1 (S1-only production): one-arm scorer support** — score_run/d2l_translate_score/sf_bt phai chay duoc config don S1 (benchmark 2-arm giu nguyen). Khong co muc nay thi one-button chi demo duoc benchmark mode — khong chap nhan.
3. **SF-BT tai phan loai COST-BEARING** -> ca score_sf_bt VA score_pj can --estimate-only + --confirm-usd (cong thuc PJ nhu §2k muc 4 + validate path + p90 pilot + multiplier 1.25; sf_bt tuong tu tren so cap can cham). score_pj --expected-db-sha256 phai truyen DONG (orchestrator tinh hash workdb mới), khong dua default MLP pin.
4. **Translator = wrapper-only cho event v1:** orchestrator phat stage_start/done/cost; sink cu giu nguyen lam forensics; KHONG thay sink trong buoc nay; bug --run-id/--attempt-id cung dest ghi nhan, sua nhan tien khi dung toi.
5. **Manifest fields mo rong (CodeX diem 4):** them argv_digest, input_artifact_sha, stage_event_log_path, stdout_log_path, workdb_sha_before/after, owner_pid, owner_host, owner_started_at. Skip stage CHI khi status=done AND artifact_sha khop AND input_artifact_sha khop AND argv_digest khop.
6. **C3.5 fallback minh bach:** uu tien notebook_decollided.json, khong co thi notebook_promoted.json + manifest ghi decollision_status; cam chon path ngam.
7. **Helper `read_jsonl_events(path, *, offset=0, max_bytes=262144) -> {events, offset, truncated, partial_line, max_bytes}` dat tai `pipeline/lib/event_reader.py`**; read_events HTTP giu validate path duoi run_events/ roi goi helper; orchestrator goi truc tiep cho stage logs.
8. **Windows policy = pseudo-code CodeX vong 2:** spawn CREATE_NEW_PROCESS_GROUP; manifest owner_pid/host/started_at/argv_digest; cancel = taskkill /PID /T /F -> doi exit -> doi workdb unlock (retry/backoff) -> manifest cancelled; resume: stage running + owner chet -> failed_resumable, owner song + heartbeat cu -> gate_pause possibly_zombie va CAM writer thu hai; sau kill khong chay scorer ngay.
9. **Retry (§2j J6 + vong 1):** cascade = cost-bearing khi GPT fallback -> khong auto-retry; chi retry-1-lan cho stage local deterministic; config/preflight fail khong retry.

**THI CONG 2 DOT (giu review de tho):**
- **O1 (prereq, offline-testable):** one-arm support cho score_run/d2l_translate_score/score_sf_bt; --estimate-only + --confirm-usd cho score_sf_bt + score_pj (validate path, khong API); pipeline/lib/event_reader.py + refactor read_events dung chung; sua §3 file nay (dong "co lap subprocess nhu score_sf_qe" — SAI) thanh "score_sf_qe chay tron duoi py311". STOP.
- **O2 (orchestrator):** run_one_button.py theo §2j+§2l du 9 muc; RunControl them run_one_button (ALLOWLIST + ESTIMATE_PREVIEW tong hop per-stage); registry attempt fields. Nghiem thu nhu §2j: run chuong co cache + kill-resume + validator merged log + Console live + frozen nguyen ven. STOP.

## 2m. O1 NGHIEM THU (Claude verify doc lap, 2026-07-06) — DAT, commit cung muc nay

**Tu chay lai toan bo bang chung:** 440/440 test pass (164s). Regression 2-arm Claude TU tai lap tren workdb (score_run --profile technical_d2l_v1 --gold-variants): B S0 0.758037 / B S1 0.765651 / D S0 0.759036 / D S1 0.825301 / A S1 0.766378 — khop metrics_mlp.json da commit TUNG CHU SO. S1-only chay sach, so S1 y het luot 2-arm (one-arm khong lam meo thuoc). Frozen 64D989 / workdb 922293 nguyen ven; key-scan sach.

**Estimate-only verify 2 chieu (phat hien quan trong ve cache path):** luot dau Claude truyen --cache-db data/jobs/pj_cache.sqlite3 -> cache_hits 0, estimate $0.817 (678 call x p90 $0.0012 — TRUNG voi cost that cua full 339 hoi truoc ~$0.8, cong thuc p90 chuan). Luot hai voi cache DEFAULT (data/reports/exp_s0s1_builderv2_v1/sf_bt_pilot_cache.sqlite3, 4353 dong): PJ 678/678 hits -> fresh 0 -> $0.00; sf_bt S1: 100 judge hits, 0 fresh, $0.00 + cache_note bao thu trung thuc (BT thieu = coi la fresh). **BAI HOC GHI VAO O2: cost estimate NHAY CAM voi cache path — orchestrator PHAI truyen dung cache db tung stage, sai path = panel bao gia sai ca chuc lan; day cung la ly do estimate_by_stage phai ghi ro cache db path da probe.**

**Ghi nhan them:** score_pj bo default pin hash MLP (khong truyen --expected-db-sha256 = khong check — orchestrator LUON truyen dong); --confirm-usd bat buoc cho run that (breaking co chu dich voi workflow cu); event_reader.py parse_error khong con mang raw line (chi error type — an toan hon, khac endpoint cu, chap nhan); helper drop dong JSON khong-phai-dict (dung hop dong envelope). CodeX tu khai near-miss --gold-variants o regression dau va tu sua — dung ky luat.

**O1 DAT -> phat prompt O2 (run_one_button + RunControl estimate tong hop + registry attempt fields).**

## 2n. CONSOLE INTERACTION CONTRACT + lich cac buoc sau O2 (user chot 2 cau hoi + nguyen tac hieu ung, 2026-07-06)

**Stream:** live theo nhip event (poll 1.4s + flush 0.5-1s = tre 1-3s, mat nguoi = live). KHONG token-stream (tai khang dinh §2c). Don vi nho nhat = block/window tick + progress {done/total}.

**Live-detail per stage:** Translator = block_done mang block_id -> preview pane fetch ban dich tu workdb qua endpoint read-only (F5). Builder = v1 chi co progress windows tu orchestrator (builder chua co emitter — §2l khong gom wiring); muc "so tay lon dan" (+N entries: ten 3-5 term, <=200 chars) DOI builder_v2_pilot nhan --event-log nhu reelection da lam -> xep vao luot UI-3 sau O2, thay doi nho co kiem soat.

**Interaction contract (nguyen tac: XEM tu do — SUA tai diem dung — KHONG SUA khi dang bay):**
- Xem moi luc: event/cost/cache/block/artifact; prompt full qua Cockpit Inspector (event chi mang sha).
- Sua truoc run: panel xac nhan (budget, S0, model). Sua trong run: CAM giua stage (argv da bind digest, sua = pha reproducibility + vo resume); chi tai gate_pause (vd tang budget roi resume).
- **PAUSE = "dung sau stage nay" (BO SUNG SCOPE O2):** control-flag file canh manifest, orchestrator check tai ranh gioi stage -> gate_pause paused_by_user, resume binh thuong. Dung giua stage = CANCEL (taskkill tree, da co O2). Duyet watchlist §36 tai gate nhu da khoa.

**Hieu ung (user chot, luot polish SAU O2):** dong event/status ngan = typewriter (gia cam giac stream nhu log agent); block preview dai = typewriter toc do cao HOAC hieu ung khac; **LUAT: moi phan dung MOT hieu ung, cam chong cheo.** Thuan CSS/JS, khong dung tang data.

**Model panel (user chot: SAU nut [DICH], TRUOC demo Thay — buoc rieng co review):** 2 bang: providers (them LM Studio endpoint/OpenAI/Gemini + nut Test tai dung preflight_check) + stage->model (tang nao dung model nao, chon tu danh sach da dang ky). RANG BUOC KHOA HOC: nhan "cau hinh da kiem dinh" (gemma-4-12b T3 103/103, pj gemini-2.5-flash da calibrate, prompt sha pin) vs "tuy chinh — so do KHONG so sanh duoc voi baseline luan van"; config-hash ghi vao manifest + report de moi ket qua tu khai chay bang gi.

**Extras chot cho buoc UI cuoi/polish:** ETA per stage (tu progress + toc do trung binh), notification khi phase_1_done/run_done (title blink/browser notification), nut "Xuat ho so run" (zip event log + manifest + report — phu luc luan van).

**Lich sau O2:** O2 (dang lam, + pause-flag) -> UI-2 nut [DICH] + panel xac nhan + auto-chuyen Console + notify 2 dot + pause/cancel/resume buttons -> renderer 2-phase -> UI-3 model panel + builder live-notebook + polish hieu ung -> demo Thay.
