/* UI-only fixtures for report_dev.html. Never load this file from index.html. */
window.REPORT_DEV_SCENARIOS = [
  {
    id: "empty",
    label: "Empty / waiting",
    run: {
      run_id: "fixture-run-empty",
      status: "running",
      started_at: "2026-07-20T08:05:00+07:00",
    },
    report: null,
  },
  {
    id: "partial",
    label: "Partial contract",
    run: {
      run_id: "fixture-run-partial",
      status: "running",
      started_at: "2026-07-20T08:12:00+07:00",
    },
    report: {
      fixture_only: true,
      schema_version: "report_shell_fixture_v1",
      contract_status: "partial",
      contract_label: "PARTIAL",
      status_message: "Một số producer đã phát hành facts; các section còn lại vẫn chờ contract.",
      generated_at: "2026-07-20T08:14:21+07:00",
      source_label: "fixture · partial producer set",
      summary: {
        state: "partial",
        title: "Báo cáo đang được hình thành",
        description: "Fixture kiểm tra trạng thái report có dữ liệu từng phần khi run chưa hoàn tất.",
        verdict: { state: "not_issued", label: "CHƯA CÓ PHÁN QUYẾT", reasons: [] },
        facts: [
          { label: "Producer ready", value: "Input Normalization", source: "fixture://normalization" },
          { label: "Producer pending", value: "Evaluation", source: "fixture://evaluation" },
        ],
      },
      coverage: {
        state: "partial",
        message: "Số liệu minh họa do fixture cung cấp nguyên trạng.",
        facts: [
          { label: "Admitted blocks", value: 34, unit: "blocks", source: "fixture://admission" },
          { label: "Expected blocks", value: 72, unit: "blocks", source: "fixture://admission" },
          { label: "Coverage state", value: "partial", source: "fixture://admission" },
        ],
      },
      quality: { state: "pending", message: "Evaluation chưa phát hành metrics." },
      comparison: { state: "pending", message: "Chưa đủ arm để công bố comparison." },
      findings: { state: "pending", message: "Terminology và Literary chưa phát hành findings." },
      execution_evidence: { state: "pending", message: "Execution manifest chưa được đóng." },
      provenance: {
        state: "partial",
        message: "Source identity đã có; artifact lineage còn thiếu.",
        facts: [
          { label: "Source identity", value: "fixture-source-001", source: "fixture://source" },
          { label: "Admission state", value: "accepted", source: "fixture://admission" },
        ],
      },
      artifacts: { state: "pending", message: "Artifact manifest chưa được phát hành." },
    },
  },
  {
    id: "one-arm",
    label: "One arm",
    run: {
      run_id: "fixture-run-one-arm",
      status: "done",
      started_at: "2026-07-20T08:20:00+07:00",
      finished_at: "2026-07-20T08:31:00+07:00",
    },
    report: {
      fixture_only: true,
      schema_version: "report_shell_fixture_v1",
      contract_status: "ready",
      contract_label: "READY · ONE ARM",
      status_message: "Report hợp lệ cho run một arm; không có delta hoặc claim so sánh.",
      generated_at: "2026-07-20T08:31:18+07:00",
      source_label: "fixture · full one-arm report",
      summary: {
        state: "ready",
        title: "One-arm evaluation report",
        description: "Fixture xác nhận UI không tạo baseline, candidate hoặc delta khi producer không công bố.",
        verdict: { state: "not_issued", label: "NO COMPARATIVE VERDICT", reasons: ["Run fixture chỉ có arm S1."] },
        facts: [
          { label: "Arm", value: "S1", source: "fixture://run-manifest" },
          { label: "Pipeline state", value: "done", source: "fixture://run-manifest" },
        ],
      },
      coverage: {
        state: "ready",
        message: "Coverage facts do fixture producer công bố.",
        facts: [
          { label: "Admitted blocks", value: 72, unit: "blocks", source: "fixture://coverage" },
          { label: "Translated blocks", value: 72, unit: "blocks", source: "fixture://coverage" },
          { label: "Excluded blocks", value: 0, unit: "blocks", source: "fixture://coverage" },
        ],
      },
      quality: {
        state: "ready",
        message: "Các giá trị dưới đây là chuỗi fixture, không được App UI tính lại.",
        metrics: [
          { key: "TC", label: "Term Consistency", value: "0.930", unit: "ratio [0,1]", definition: "Fixture definition for layout QA.", scope: "run", direction: "higher", source: "fixture://evaluation/metrics" },
          { key: "SF-QE", label: "Semantic Fidelity QE", value: "0.884", unit: "model score", definition: "Fixture definition for layout QA.", scope: "translated blocks", direction: "higher", source: "fixture://evaluation/metrics" },
        ],
      },
      comparison: {
        state: "one_arm",
        message: "Producer xác nhận run chỉ có S1; comparison không áp dụng.",
        baseline: "",
        candidate: "S1",
        metrics: [],
      },
      findings: { state: "empty", message: "Fixture producer xác nhận không có finding trong scope." },
      execution_evidence: {
        state: "ready",
        message: "Facts thực thi được cung cấp trực tiếp bởi fixture manifest.",
        facts: [
          { label: "Run mode", value: "single-arm", source: "fixture://run-manifest" },
          { label: "Terminal state", value: "done", source: "fixture://run-manifest" },
        ],
      },
      provenance: {
        state: "ready",
        facts: [
          { label: "Source digest", value: "fixture-sha256-source", source: "fixture://provenance" },
          { label: "Config digest", value: "fixture-sha256-config", source: "fixture://provenance" },
        ],
      },
      artifacts: {
        state: "ready",
        items: [
          { label: "One-arm report", path: "fixtures/report_shell_v1/one_arm_report.json", kind: "report", status: "fixture" },
        ],
      },
    },
  },
  {
    id: "comparison",
    label: "Comparison ready",
    run: {
      run_id: "fixture-run-comparison",
      status: "done",
      started_at: "2026-07-20T08:40:00+07:00",
      finished_at: "2026-07-20T09:03:00+07:00",
    },
    report: {
      fixture_only: true,
      schema_version: "report_shell_fixture_v1",
      contract_status: "ready",
      contract_label: "READY",
      status_message: "Tất cả section fixture đã có payload hợp lệ để kiểm tra bố cục.",
      generated_at: "2026-07-20T09:03:24+07:00",
      source_label: "fixture · full comparison report",
      summary: {
        state: "ready",
        title: "S0 / S1 Full Run Report",
        description: "Khung báo cáo tổng hợp coverage, quality, comparison, findings và provenance từ các producer độc lập.",
        verdict: {
          state: "pass",
          label: "PASS",
          reasons: ["Fixture gate set reports pass."],
          source: "fixture://evaluation/verdict",
        },
        facts: [
          { label: "Baseline", value: "S0", source: "fixture://evaluation/comparison" },
          { label: "Candidate", value: "S1", source: "fixture://evaluation/comparison" },
          { label: "Reported gates", value: "7 / 7", status: "pass", source: "fixture://evaluation/gates" },
        ],
      },
      coverage: {
        state: "ready",
        message: "Counts được giữ ở dạng facts, không gộp thành coverage score.",
        facts: [
          { label: "Source chapters", value: 4, unit: "chapters", source: "fixture://normalization/coverage" },
          { label: "Admitted blocks", value: 72, unit: "blocks", source: "fixture://normalization/coverage" },
          { label: "S0 translated", value: 72, unit: "blocks", source: "fixture://evaluation/coverage" },
          { label: "S1 translated", value: 72, unit: "blocks", source: "fixture://evaluation/coverage" },
        ],
      },
      quality: {
        state: "ready",
        message: "Không có composite score; mỗi metric giữ definition, scope, unit và source riêng.",
        metrics: [
          { key: "TC-S1", label: "Term Consistency · S1", value: "0.930", unit: "ratio [0,1]", status: "good", definition: "Consistency across localized term occurrences.", scope: "S1 translated blocks", direction: "higher", source: "fixture://terminology/metrics" },
          { key: "TA-S1", label: "Term Adherence · S1", value: "0.875", unit: "ratio [0,1]", status: "good", definition: "Adherence to accepted term forms reported by the fixture producer.", scope: "S1 term occurrences", direction: "higher", source: "fixture://terminology/metrics" },
          { key: "SF-QE", label: "Semantic Fidelity QE", value: "0.884", unit: "model score", status: "reported", definition: "Reference-free fixture evidence; not a sole verdict.", scope: "S1 translated blocks", direction: "higher", source: "fixture://evaluation/sf-qe" },
          { key: "LIT-EVID", label: "Literary Evidence Coverage", value: "41 / 44", unit: "evidence rows", status: "warn", definition: "Fixture count of literary findings with persisted evidence.", scope: "literary review set", direction: "descriptive", source: "fixture://literary/evidence" },
        ],
      },
      comparison: {
        state: "ready",
        message: "Baseline, candidate và delta đều do fixture producer công bố nguyên trạng.",
        baseline: "S0",
        candidate: "S1",
        metrics: [
          { key: "TC", label: "Term Consistency", baseline: "0.820", candidate: "0.930", delta: "+0.110", unit: "reported gap", status: "good", source: "fixture://evaluation/comparison" },
          { key: "TA", label: "Term Adherence", baseline: "0.861", candidate: "0.875", delta: "+0.014", unit: "reported gap", status: "good", source: "fixture://evaluation/comparison" },
          { key: "SF-QE", label: "Semantic Fidelity QE", baseline: "0.889", candidate: "0.884", delta: "-0.005", unit: "reported gap", status: "warn", source: "fixture://evaluation/comparison" },
        ],
      },
      findings: {
        state: "ready",
        message: "Chọn một finding để mở evidence drawer.",
        items: [
          {
            id: "TERM-014",
            severity: "warning",
            category: "terminology",
            title: "Một thuật ngữ còn hai surface forms",
            summary: "Fixture finding cho kiểm tra list/detail và text dài.",
            location: "block-0042",
            owner: "Terminology",
            artifact_path: "fixtures/report_shell_v1/findings/term-014.json",
            evidence: { source_term: "translation memory", reported_forms: ["bộ nhớ dịch", "bộ nhớ bản dịch"], occurrences: 3 },
          },
          {
            id: "LIT-006",
            severity: "info",
            category: "literary",
            title: "Nhịp câu cần evidence bổ sung",
            summary: "Producer chỉ báo finding; UI không tự gắn verdict.",
            location: "block-0057",
            owner: "Literary",
            artifact_path: "fixtures/report_shell_v1/findings/lit-006.json",
            evidence: { evidence_state: "partial", note: "fixture-only evidence" },
          },
        ],
      },
      execution_evidence: {
        state: "ready",
        message: "Execution facts dùng để audit, không thay thế live Console.",
        facts: [
          { label: "Run state", value: "done", source: "fixture://run-manifest" },
          { label: "Reported API calls", value: 18, unit: "calls", source: "fixture://execution/calls" },
          { label: "Reported cache hits", value: 7, unit: "calls", source: "fixture://execution/cache" },
          { label: "Reported cost", value: "0.1842", unit: "USD", source: "fixture://execution/cost" },
        ],
      },
      provenance: {
        state: "ready",
        message: "Identity và digest là chuỗi fixture để kiểm tra wrapping.",
        facts: [
          { label: "Source digest", value: "sha256:fixture-source-9a63d4c1", source: "fixture://normalization/provenance" },
          { label: "Admission digest", value: "sha256:fixture-admission-4402d07e", source: "fixture://normalization/provenance" },
          { label: "Runtime profile", value: "fixture_profile_v1", source: "fixture://coordinator/manifest" },
          { label: "Report contract", value: "report_shell_fixture_v1", source: "fixture://coordinator/contract" },
        ],
      },
      artifacts: {
        state: "ready",
        message: "Danh sách fixture mô phỏng artifact manifest đã persisted.",
        items: [
          { label: "Full run report", path: "fixtures/report_shell_v1/reports/full_run_report.json", kind: "report", status: "fixture", digest: "sha256:fixture-report" },
          { label: "Terminology findings", path: "fixtures/report_shell_v1/reports/terminology_findings.json", kind: "findings", status: "fixture" },
          { label: "Literary evidence", path: "fixtures/report_shell_v1/reports/literary_evidence.json", kind: "evidence", status: "fixture" },
        ],
      },
    },
  },
  {
    id: "invalid",
    label: "Invalid payload",
    run: {
      run_id: "fixture-run-invalid",
      status: "done",
      started_at: "2026-07-20T09:10:00+07:00",
      finished_at: "2026-07-20T09:15:00+07:00",
    },
    report: {
      fixture_only: true,
      schema_version: "report_shell_fixture_v1",
      contract_status: "invalid",
      contract_label: "INVALID",
      status_message: "Payload fixture vi phạm contract; UI phải fail-closed.",
      source_label: "fixture · invalid report",
      validation_errors: [
        "summary.verdict.source is required when a verdict is issued",
        "comparison.metrics[0].candidate is missing",
      ],
    },
  },
];
