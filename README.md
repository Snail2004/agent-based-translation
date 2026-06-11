# Agent-Based Long-Document Translation Research

Thu muc khoa luan: agent-based long-document EN-VI translation.

## Cau truc (3 vung)

- `THESIS_ARCHITECTURE_LOCK.md` — **FILE SO 1**: so quyet dinh kien truc (da chot / chot mem / hang doi) + changelog. Moi cold-start bat dau tu day.
- `design/` — tai lieu thiet ke DANG HIEU LUC:
  - `RESEARCH_PLAN_V3.md` — ke hoach nghien cuu (RQ, metric, experiment); xem §0 Directional Lock (GVHD chot).
  - `RUN_EVAL_SCHEMA.md` — lop luu run/eval (translation_runs, evaluation_runs, reference_eval_only, context_bundle).
  - `PROMPT_DESIGN.md` — prompt contract cho cac agent.
  - `RETRIEVAL_ARCHITECTURE.md` — kien truc retrieval hard/soft (luu y cac OVERRIDE ghi trong file).
  - `SCHEMA_AGENT_FILL_POLICY.md` — fill-tier A/B/C/D cho schema 1.5.0.
  - `DATASET_DESIGN.md` — thiet ke dataset (mot phan da superseded boi LOCK §6.1: Treasure Island thay Alice).
- `reference/` — tham khao, KHONG phai quyet dinh: TECH_LEAD_REVIEW_SESSION (transcript), DATASET_DESIGN_AGENT_REFERENCE, RELATED_WORK, VERIFIED_REFERENCES, AMT_paper_extracted_research, bao_cao_phan_bien, `papers/`.
- `archive/` — ban cu (plan V1/V2, legacy-tasks). Chi doc khi can lich su.
- `THESIS_RUNTIME_TOOL/` — **nha cua code thesis**: `pipeline/` (runtime moi), `app/` (vendored donor + run-viewer tuong lai), `dataset_spec/` (schema 1.5.0 + validate), `tasks/` + `LEDGER.md` (so truy vet cong viec).
- `AILAB_HANDOFF/` (nested git repo), `ailab/`, `AILAB_SOURCES_RAW/` — track AI-LAB, tach biet voi thesis. KHONG dua noi dung thesis vao day.

## Cold-start (cho agent moi)

1. Doc `THESIS_ARCHITECTURE_LOCK.md` (quyet dinh + trang thai).
2. Doc `THESIS_RUNTIME_TOOL/tasks/LEDGER.md` (viec nao xong/mo, commit nao).
3. Mo dung TASK dang lien quan. Het.

Doc `design/` khi can chi tiet ky thuat; `reference/` chi de doi chieu; khong doc `archive/`.

## Scope Guardrail

Huong nghien cuu chinh la memory/retrieval-centric narrative translation cho van ban dai Anh-Viet. PDF/layout/OCR chi la adapter, khong phai trong tam. Hai track AI-LAB vs thesis KHONG duoc tron (xem LOCK §8.1).
