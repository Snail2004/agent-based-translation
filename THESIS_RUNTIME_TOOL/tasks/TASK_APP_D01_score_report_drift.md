# TASK_APP_D01_score_report_drift — ScoreReadModel + Consistency/Drift view + report export (read-only)

- **Status:** READY
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

- **Data-source policy:** report file nào → job nào (resolver); đọc read-only, KHÔNG đụng SQLite trừ khi tra vị-trí (tùy chọn, display).
- **Read-model contract:** shape headline + drift + provenance (dán mẫu JSON thật).
- **No-recompute guard:** chứng minh UI render số TỪ report, không tự tính.
- **Known-gap note:** field provenance/locating nào thiếu + đề xuất, KHÔNG vá scorer.
- **Separation guard:** scores ⊥ dataset ⊥ observability.
- **Test plan:** fixture + lệnh + output. (kèm file đổi, gotcha.)

## 6. Review *(Claude điền)*

- **Verdict:** (trống)
- Findings: …
- Follow-up: …

---

**GATE (LOCK nn):** D01 READ-ONLY, render-từ-scorer-report. KHÔNG recompute metric trong UI, KHÔNG scorer/pipeline/engine change, KHÔNG run-control (C01). Còn lại sau D01: `APP_C01` (run control) để khép App.
