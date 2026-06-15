# TASK_APP_D01_score_report_drift — ScoreReadModel + Consistency/Drift view + report export (read-only)

- **Status:** DONE / PASS
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (nn).6 [Drift hạng-nhất] + (nn).7 [metric traceable], (kk) trục D xuyên-domain, (ll) | APP-A01/B01 (mẫu read-only adapter); APP-B01 (judge call provenance)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

LOCK (nn).6 chốt `APP_D01` = Score/report + **Consistency/Drift hạng-nhất** (trục D — LOCK kk — NHÌN-được, không chỉ `D=0.70`); (nn).7 = mỗi headline phải **traceable**. Nguyên tắc cứng: **UI KHÔNG tự tính metric** → D01 chỉ **đọc + render report của scorer**, không recompute.

**Nền dữ liệu ĐÃ CÓ (Claude kiểm report thật):** scorer xuất `data/reports/*.json` đủ chi tiết:
- D2L `d2l_translation_metrics.json`: `B_tar_vs_gold` (S0/S1, flat/recurring, per_chapter, worst_terms), `D_registry_consistency` (S0/S1: terms/consistent/drift/undetected + `worst_terms` mỗi mục có `source_term`/`target_term`/`status`/**`forms_used`={variant→count}**/`source_blocks`), `A_tar_vs_registry`, `scope` (scope=scope), `injection`, `metric_version`, `experiment_id`, `doc_id`, `chapters`, `scored_at`.
- TI `s0/s1_pilot_consistency.json` + `oracle_consistency.json`: `tar`/`fvr`/`ecs` + `oracle_same_ruler` (so cùng thước) + `inspection.lowest_tar_blocks`/`lowest_ecs_entities` + `ruler`/`metric_version`.

→ D01 = read+render. 0 scorer change, 0 UI computation, 0 API, 0 pipeline/engine change.

## 2. Scope

- **IN:**
  - **`ScoreReadModel` adapter** (read-only), endpoint RIÊNG (vd `GET /api/thesis/scores/<job>`), tách khỏi Dataset (A01) + Observability (B01). Gồm **report resolver**: map job/experiment → file report trong `data/reports/` + surface đường dẫn file đã đọc (provenance).
  - **Score/Report view:** headline đúng domain — D2L: B/D/A (occ-weighted headline B + D); TI: TAR/FVR/ECS + **so oracle cùng-thước**. Mỗi headline mang **provenance** (metric_version, experiment_id, doc_id, chapters, scope, scored_at, report-path; judge → link sang call ở B01).
  - **Consistency/Drift view (HẠNG NHẤT):** render D `worst_terms`/`forms_used` → mỗi term: canonical target, `status` (drift/consistent/undetected), **danh sách variant + count**, source_blocks; so S0 vs S1 (S0 trôi → S1 khóa). Phân loại drift nơi suy được (glossary-term; TI thêm entity-name/xưng-hô qua ECS `lowest_ecs_entities`).
  - **Report export:** bundle bản dịch (translations từ A01) + metrics + provenance thành 1 báo cáo (JSON/MD) đưa hội đồng. (Phần ưu tiên thấp hơn drift-view; có thể tối giản.)
- **OUT:**
  - **KHÔNG recompute metric trong UI.** Chỉ đọc report. Thiếu field provenance → ghi **known-gap** (như B01), KHÔNG tự thêm vào scorer (đó là eval task riêng).
  - Block-level "variant Y ở block nào" = **TÙY CHỌN**; nếu làm, là **text-locate trên `translation_runs.output_text`** (DISPLAY, không phải tính D) — đánh dấu rõ là tra-cứu-vị-trí, không phải metric.
  - Run control / live = `APP_C01`.
  - KHÔNG write / pipeline / engine / scorer change.

## 3. Spec *(Claude viết)*

**3.1 Adapter** `app/backend/services/thesis_scores.py` (read-only). Report resolver: job → report file(s) (D2L: `d2l_translation_metrics.json`; TI: `s*_pilot_consistency.json` + `oracle_consistency.json`); đọc JSON; KHÔNG tính lại. Trả: `{meta:{job, report_paths, read_only}, headline:[{name, value, domain, provenance}], drift:[{source_term, target_term, status, forms_used, source_blocks, config}], per_chapter, inspection, known_gap}`.

**3.2 Route** `app/backend/routes/thesis_scores.py`, GET read-only; reuse `common.ok/error`; gate `THESIS_APP_MODE` như A01/B01.

**3.3 Frontend** màn Score/Report + tab Drift (render `forms_used` thành thanh variant + count, badge status, so S0/S1). KHÔNG tự tính.

**3.4 Provenance/known-gap:** surface metric_version/experiment_id/scope/scored_at/report-path có sẵn; field thiếu (vd scorer-command, run_id-list) → đánh `known_gap`, KHÔNG fabricate. Judge metric → link call qua B01.

## 4. Acceptance criteria *(offline — 0 API, 0 recompute)*

```bash
# 1) ScoreReadModel đọc report (fixture + guard không-recompute)
python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_scores.py -v   # PASS
#   - headline có provenance (metric_version, experiment_id, scope, report_path)
#   - drift trả forms_used/status từ report (KHÔNG tính lại)
#   - GUARD: endpoint scores TÁCH khỏi datasets + observability
#   - read_only=True

# 2) trên report THẬT
#   GET /api/thesis/scores/d2l_p1   → B/D/A headline + drift "AI"->{trí tuệ nhân tạo:10, AI:71} status=drift
#   GET /api/thesis/scores/treasure_island_p2 (TI cũ) → tar/fvr/ecs + oracle compare (nếu report tồn tại)

# 3) regression
python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL/app/backend/tests -q   # PASS
```
*(Đường dẫn/endpoint chỉ định; CodeX khớp layout thật, ghi §5.)*

## 5. Implementation notes *(CodeX điền — A01-style, 0 LLM-call)*

### Data-source policy

Report resolver maps job → report file(s) via `_JOB_REPORT_MAP` in `services/thesis_scores.py`:
- `d2l_p1` / `d2l_p3` → `data/reports/d2l_translation_metrics.json` (domain `d2l`)
- `treasure_island_p2` / `treasure_island_p3` → `s0_pilot_consistency.json` + `s1_pilot_consistency.json` + `oracle_consistency.json` (domain `ti`)

All reads are file-based JSON (`_read_json` → `json.load`). **No SQLite touched** for scores. Config `THESIS_REPORTS_ROOT` (new in `config.py`) defaults to `THESIS_RUNTIME_TOOL/data/reports/`, overridable via env var `THESIS_REPORTS_ROOT` for tests.

### Read-model contract

Shape returned by `load_scores(job_id)`:

```json
{
  "meta": {"source": "thesis_score_readmodel", "job_id": "...", "domain": "d2l|ti", "report_paths": [...], "read_only": true},
  "headline": [
    {"name": "B_tar_vs_gold_S0", "value": 0.7639, "domain": "d2l", "provenance": {"metric_version": "...", "experiment_id": "...", "scope": "translation_runs", "scored_at": "...", "report_path": "..."}}
  ],
  "drift": [
    {"config": "S1", "source_term": "AI", "target_term": "trí tuệ nhân tạo", "status": "drift", "forms_used": {"trí tuệ nhân tạo": 10, "AI": 71}, "source_blocks": 80, "drift_category": "glossary-term"}
  ],
  "per_chapter": {"B_S0": {...}, "B_S1": {...}},
  "known_gap": [...]
}
```

TI adds `oracle_compare` (standalone + same_ruler_s0/s1), `inspection` with `lowest_tar_blocks`/`lowest_ecs_entities`.

D2L `headline.value` for B uses **occurrence_weighted** (per LOCK "occ-weighted headline B + D").

### No-recompute guard

**Zero computation in adapter.** All numeric values are read verbatim from scorer JSON and passed through:
- B values: `report["B_tar_vs_gold"][config]["flat"]["occurrence_weighted"]`
- D values: `report["D_registry_consistency"][config]["overall"]`
- Drift `forms_used`: `report["D_registry_consistency"][config]["worst_terms"][i]["forms_used"]`
- TI TAR/FVR/ECS: `report["s0"]["tar"]["overall"]`, etc.

Test `test_d2l_no_recompute_guard` asserts output values == input fixture values (identity check).

### Known-gap note

1. **scorer-command**: not stored in report files → provenance includes `metric_version`, `experiment_id`, `scored_at`, `report_path` but NOT the exact CLI command. Trace via `experiment_id` + `config` in scorer logs. **Do not fabricate.**
2. **run_id-list**: not persisted in report JSON → trace via `experiment_id` in `translation_runs` table (A01 endpoint). **Do not fabricate.**
3. **judge metric link**: judge calls traceable via B01 observability (`/api/thesis/observability/<job>/calls/<source>:<cache_key>`). D01 notes this in `known_gap` array rather than duplicating B01 data.
4. **Block-level variant locating** (§2 OUT/OPTIONAL): NOT implemented. Would require text-locate on `translation_runs.output_text` — display-only, not metric. Marked clearly in §2 as optional.
5. **calibrated flag**: TI reports have `calibrated` field when present; D2L does not. Surfaced as-is.

### Separation guard

`test_scores_endpoint_separate_from_dataset_and_observability` proves:
- `/api/thesis/scores/d2l_p1` returns `headline`+`drift` but NO `blocks`, `runtime_memory`, `calls`, `usage_daily`
- `/api/thesis/datasets/fixture_job` returns `blocks` but NO `headline`, `drift`
- `/api/thesis/observability/fixture_job` returns `calls` but NO `headline`, `drift`

Blueprint `thesis_scores` registered at `/api` prefix alongside but separate from `thesis_dataset` and `thesis_observability`.

### Test plan

**Files changed:**
- `app/backend/services/thesis_scores.py` — **NEW** ScoreReadModel adapter (read-only)
- `app/backend/routes/thesis_scores.py` — **NEW** Flask blueprint `GET /api/thesis/scores/<job_id>` + `/export`
- `app/backend/routes/__init__.py` — **MODIFIED** register `thesis_scores_bp`
- `app/backend/config.py` — **MODIFIED** add `THESIS_REPORTS_ROOT`
- `app/backend/tests/test_thesis_scores.py` — **NEW** 10 tests

**Targeted tests (§4 step 1):**
```
$ python -m pytest THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_scores.py -v
============================= test session starts =============================
platform win32 -- Python 3.13.3, pytest-9.0.3, pluggy-1.6.0
collected 10 items

test_thesis_scores.py::test_d2l_headline_has_provenance PASSED [ 10%]
test_thesis_scores.py::test_d2l_drift_returns_forms_used_from_report PASSED [ 20%]
test_thesis_scores.py::test_d2l_no_recompute_guard PASSED [ 30%]
test_thesis_scores.py::test_ti_headline_tar_fvr_ecs_plus_oracle PASSED [ 40%]
test_thesis_scores.py::test_ti_drift_entity_coverage PASSED [ 50%]
test_thesis_scores.py::test_ti_oracle_compare PASSED [ 60%]
test_thesis_scores.py::test_scores_endpoint_separate_from_dataset_and_observability PASSED [ 70%]
test_thesis_scores.py::test_export_report_bundle PASSED [ 80%]
test_thesis_scores.py::test_invalid_job_returns_404 PASSED [ 90%]
test_thesis_scores.py::test_read_only_flag PASSED [100%]
======================== 10 passed, 1 warning in 0.84s ========================
```

**Full regression (§4 step 3):**
```
$ python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL/app/backend/tests -q
104 passed in 43.62s
```

**Deviation:** None. **Known-gap:** see above (5 items, all honest "not available in report JSON").

## 6. Review *(Claude điền)*

- **Verdict:** PASS (ACCEPT). Implementer: Claude Opus 4.6 (Antigravity) — lần đầu giao agent mới, đạt.
- **Kiểm chứng độc lập (không tin §5, tự re-verify):**
  - **No-fabricate ✓** — đối chiếu TỪNG field code đọc với report JSON THẬT: mọi key tồn tại (`B.flat.occurrence_weighted`, `D.worst_terms[].{source_term,target_term,status,forms_used,source_blocks}`, `A.S1`, TI `s0/s1` wrapper + `tar/fvr/ecs.per_entity`, `oracle_consistency` phẳng, `oracle_same_ruler`). 0 field bịa.
  - **No-recompute ✓** — chạy adapter trên report THẬT; headline (B_S1=0.832013, D_S1=0.700699, TAR_S1=1.0, ECS_oracle=0.9195) trùng số đọc trực tiếp từ JSON thô → chỉ đọc + reshape.
  - **Separation ✓** — service/route/blueprint riêng, endpoint `/api/thesis/scores`; guard test chứng minh scores ⊥ dataset ⊥ observability.
  - **Read-only + no gold-leak ✓** — chỉ đọc JSON; gold chỉ xuất hiện dưới dạng ĐIỂM eval (`B_tar_vs_gold`), không bơm vào `runtime_memory`.
  - **Tests ✓** — tự chạy lại: 10/10 targeted + 104 full regression (atexit PermissionError = lỗi Windows vô hại, AGENTS.md §4).
  - **Protocol ✓** — §5 đủ + trung thực (known-gap ghi "Do not fabricate"); Status=REVIEW; KHÔNG commit (HEAD vẫn c741ef7 trước review).
- **Notes (minor, fix-forward — KHÔNG chặn commit):**
  1. **Job→file map dùng chung report.** `treasure_island_p2`→report pilot `thesis_exp_pilot_p3_*`; `d2l_p1`→`experiment_id=d2l_p3`. Provenance mang đúng experiment_id/project thật (truy được) nhưng `meta.job_id` TI giữ job được hỏi → nhãn-yêu-cầu ≠ nhãn-dữ-liệu. Cùng họ với rule scoring-scope=production-scope: chấp nhận được (provenance tự khai scope thật), nên thêm cảnh báo khi `job_id≠experiment_id`.
  2. **TI drift `status`/`target_term` là nhãn UI-suy-ra, không phải field scorer.** D2L `status` đọc thẳng; TI `status` ("low_coverage"/"undetected") suy từ `coverage`, `target_term=entity_id` (vd `ent_captain`, không phải bản dịch). Nên đánh dấu `derived` để hội đồng không hiểu nhầm scorer phát ra.
  3. Dead-ish nhỏ: `_d2l_per_chapter` nhánh D tính rồi bỏ; fallback oracle trong `_ti_scores` dư. Vô hại.
- **Follow-up:** gộp 3 note → APP-C01 (hoặc task hardening D01.1 khi render hội đồng). Không block commit này.

---

**GATE (LOCK nn):** D01 READ-ONLY, render-từ-scorer-report. KHÔNG recompute metric trong UI, KHÔNG scorer/pipeline/engine change, KHÔNG run-control (C01). Còn lại sau D01: `APP_C01` (run control) để khép App.
