/* Run Report shell — read-only presentation surface for persisted run facts.
   Production currently receives the existing report-summary read model only.
   Missing sections remain explicitly unavailable until their owning pipeline
   publishes an accepted contract; this component never derives new metrics. */

const REPORT_SECTION_DEFS = Object.freeze([
  { id: "summary", index: "01", vi: "Tổng quan", en: "Summary", shortVi: "Tổng quan", shortEn: "Summary", owner: "Coordinator + Evaluation" },
  { id: "coverage", index: "02", vi: "Phạm vi", en: "Coverage", shortVi: "Phạm vi", shortEn: "Coverage", owner: "Input Normalization" },
  { id: "quality", index: "03", vi: "Chất lượng", en: "Quality", shortVi: "Chất lượng", shortEn: "Quality", owner: "Evaluation + domain pipelines" },
  { id: "comparison", index: "04", vi: "So sánh", en: "Comparison", shortVi: "So sánh", shortEn: "Comparison", owner: "Evaluation" },
  { id: "findings", index: "05", vi: "Phát hiện", en: "Findings", shortVi: "Phát hiện", shortEn: "Findings", owner: "Terminology + Literary" },
  { id: "execution", index: "06", vi: "Bằng chứng chạy", en: "Execution evidence", shortVi: "Thực thi", shortEn: "Execution", owner: "Coordinator" },
  { id: "provenance", index: "07", vi: "Nguồn gốc", en: "Provenance", shortVi: "Nguồn gốc", shortEn: "Provenance", owner: "Input Normalization + Coordinator" },
  { id: "artifacts", index: "08", vi: "Tệp đầu ra", en: "Artifacts", shortVi: "Đầu ra", shortEn: "Artifacts", owner: "All producers" },
]);

const REPORT_TERMINAL_STATUSES = new Set(["done", "failed", "cancelled", "canceled", "error"]);
const REPORT_ALLOWED_STATES = new Set(["ready", "partial", "pending", "unavailable", "invalid", "one_arm", "empty"]);
const REPORT_STATUS_META = Object.freeze({
  ready: { vi: "SẴN SÀNG", en: "READY", tone: "good", glyph: "●" },
  partial: { vi: "MỘT PHẦN", en: "PARTIAL", tone: "warn", glyph: "◐" },
  pending: { vi: "ĐANG CHỜ", en: "PENDING", tone: "info", glyph: "◌" },
  unavailable: { vi: "KHÔNG KHẢ DỤNG", en: "UNAVAILABLE", tone: "muted", glyph: "—" },
  invalid: { vi: "KHÔNG HỢP LỆ", en: "INVALID", tone: "bad", glyph: "×" },
  one_arm: { vi: "MỘT NHÁNH", en: "ONE ARM", tone: "info", glyph: "Ⅰ" },
  empty: { vi: "TRỐNG", en: "EMPTY", tone: "muted", glyph: "○" },
});

function reportIsObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function reportArray(value) {
  return Array.isArray(value) ? value : [];
}

function reportState(value, fallback = "unavailable") {
  const normalized = String(value || "").toLowerCase();
  return REPORT_ALLOWED_STATES.has(normalized) ? normalized : fallback;
}

function reportStatusMeta(state) {
  const meta = REPORT_STATUS_META[reportState(state)] || REPORT_STATUS_META.unavailable;
  return { ...meta, label: uiText(meta.vi, meta.en) };
}

function reportSectionLabel(definition) {
  return uiText(definition.vi, definition.en);
}

function reportSectionShortLabel(definition) {
  return uiText(definition.shortVi, definition.shortEn);
}

function reportTerminal(status) {
  return REPORT_TERMINAL_STATUSES.has(String(status || "").toLowerCase());
}

function reportText(value, fallback = "") {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function reportFormatValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "—";
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 }).format(value);
  }
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function reportFormatTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString(ThesisI18n.getLocale() === "en" ? "en-US" : "vi-VN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function reportNormalizeFact(row, index) {
  if (!reportIsObject(row)) {
    return { id: `fact-${index}`, label: `Fact ${index + 1}`, value: reportText(row, "—"), source: "" };
  }
  return {
    id: reportText(row.id, `fact-${index}`),
    label: reportText(row.label || row.key, `Fact ${index + 1}`),
    value: row.value,
    unit: reportText(row.unit),
    note: reportText(row.note || row.description),
    source: reportText(row.source || row.artifact_path),
    status: reportText(row.status),
  };
}

function reportNormalizeMetric(row, index) {
  if (!reportIsObject(row)) return null;
  return {
    id: reportText(row.id || row.key, `metric-${index}`),
    key: reportText(row.key || row.id, `metric-${index}`),
    label: reportText(row.label || row.name, reportText(row.key || row.id, `Metric ${index + 1}`)),
    value: row.value,
    unit: reportText(row.unit),
    status: reportText(row.status),
    definition: reportText(row.definition || row.description),
    scope: reportText(row.scope),
    direction: reportText(row.direction),
    source: reportText(row.source || row.artifact_path || row.report_path),
  };
}

function reportNormalizeComparisonMetric(row, index) {
  if (!reportIsObject(row)) return null;
  return {
    id: reportText(row.id || row.key, `comparison-${index}`),
    key: reportText(row.key || row.id, `comparison-${index}`),
    label: reportText(row.label || row.name, reportText(row.key || row.id, `Metric ${index + 1}`)),
    baseline: row.baseline,
    candidate: row.candidate,
    delta: row.delta !== undefined ? row.delta : row.gap,
    unit: reportText(row.unit),
    status: reportText(row.status),
    source: reportText(row.source || row.artifact_path),
  };
}

function reportNormalizeFinding(row, index) {
  if (!reportIsObject(row)) return null;
  return {
    id: reportText(row.id, `finding-${index + 1}`),
    severity: reportText(row.severity || row.status, "info").toLowerCase(),
    category: reportText(row.category || row.kind, "reported"),
    title: reportText(row.title || row.label || row.source_term, `Finding ${index + 1}`),
    summary: reportText(row.summary || row.description),
    location: reportText(row.location || row.block_id),
    owner: reportText(row.owner),
    artifactPath: reportText(row.artifact_path || row.source),
    evidence: row.evidence !== undefined ? row.evidence : row.by_config,
    meta: reportIsObject(row.meta) ? row.meta : null,
  };
}

function reportNormalizeArtifact(row, index) {
  if (typeof row === "string") {
    return { id: `artifact-${index}`, label: row.split(/[\\/]/).pop() || row, path: row, kind: "artifact", status: "reported" };
  }
  if (!reportIsObject(row)) return null;
  return {
    id: reportText(row.id, `artifact-${index}`),
    label: reportText(row.label || row.name, reportText(row.path, `Artifact ${index + 1}`)),
    path: reportText(row.path || row.artifact_path),
    kind: reportText(row.kind || row.type, "artifact"),
    status: reportText(row.status, "reported"),
    digest: reportText(row.digest || row.sha256),
    note: reportText(row.note || row.description),
  };
}

function reportEmptySection(message) {
  return { state: "unavailable", message };
}

function reportLegacySummary(raw, context) {
  const phaseOne = reportIsObject(raw.phase_1) && raw.phase_1.present ? raw.phase_1 : null;
  const final = reportIsObject(raw.final) && raw.final.present ? raw.final : null;
  const comparison = reportIsObject(raw.compare) && raw.compare.present ? raw.compare : null;
  const consistency = reportIsObject(raw.consistency) && raw.consistency.present ? raw.consistency : null;
  const metricRows = reportArray(final?.metrics).length ? final.metrics : reportArray(phaseOne?.metrics);
  const metrics = reportArray(metricRows).map(reportNormalizeMetric).filter(Boolean);
  const verdictRaw = reportIsObject(final?.verdict) ? final.verdict : null;
  const verdict = verdictRaw && typeof verdictRaw.pass === "boolean"
    ? {
      state: verdictRaw.pass ? "pass" : "fail",
      label: verdictRaw.pass ? "PASS" : "FAIL",
      reasons: reportArray(verdictRaw.reasons).map(reason => reportText(reason)).filter(Boolean),
      source: reportText(final?.report_path),
    }
    : { state: "not_issued", label: "CHƯA CÓ PHÁN QUYẾT", reasons: [] };

  const gapRows = comparison && reportIsObject(comparison.gap)
    ? Object.entries(comparison.gap).map(([key, value]) => ({
      id: `gap-${key}`,
      key,
      label: key,
      delta: value,
      source: reportText(final?.report_path),
    }))
    : [];

  const findings = reportArray(consistency?.notable_terms).map((row, index) => reportNormalizeFinding({
    id: `term-${index + 1}`,
    category: "terminology",
    severity: "reported",
    title: row?.source_term,
    summary: row?.tier ? `tier: ${row.tier}` : "",
    evidence: row?.by_config,
    meta: { fixed_by_injection: row?.fixed_by_injection },
    artifact_path: reportText(final?.report_path || phaseOne?.report_path),
  }, index)).filter(Boolean);

  const artifacts = [
    phaseOne?.report_path ? { label: "Phase 1 score report", path: phaseOne.report_path, kind: "score-report", status: "reported" } : null,
    final?.report_path ? { label: "Final score report", path: final.report_path, kind: "score-report", status: "reported" } : null,
  ].filter(Boolean).map(reportNormalizeArtifact).filter(Boolean);

  const hasPayload = !!(phaseOne || final || comparison || consistency);
  return {
    contractStatus: hasPayload ? "partial" : (reportTerminal(context.runStatus) ? "unavailable" : "pending"),
    contractLabel: hasPayload ? "SUMMARY-ONLY" : "NO REPORT",
    statusMessage: hasPayload
      ? uiText("Đang hiển thị report-summary đã lưu. Contract Báo cáo lần chạy đầy đủ chưa được nối vào App UI.", "Showing the persisted report-summary. The Full Run Report contract is not yet connected to the App UI.")
      : reportTerminal(context.runStatus)
        ? uiText("Lần chạy đã kết thúc nhưng report-summary hiện không có dữ liệu khả dụng.", "The run finished, but report-summary currently has no available data.")
        : uiText("Lần chạy chưa phát hành report-summary; trang sẽ giữ trạng thái chờ.", "The run has not published report-summary; this page will remain pending."),
    schemaVersion: "report-summary (legacy read model)",
    generatedAt: "",
    sourceLabel: "report-summary · read-only",
    summary: {
      state: hasPayload ? "partial" : "pending",
      message: hasPayload ? uiText("Tổng quan giới hạn trong dữ liệu summary hiện có.", "The summary is limited to currently available summary data.") : uiText("Chờ report-summary.", "Waiting for report-summary."),
      verdict,
      title: uiText("Kết quả lần chạy", "Run results"),
      description: uiText("Snapshot báo cáo cuối đã lưu; không đồng bộ theo replay cursor của Console.", "The final report snapshot is persisted and does not follow the Console replay cursor."),
      facts: [
        final?.stage_gate?.present ? {
          label: "Stage gate",
          value: `${reportFormatValue(final.stage_gate.passed)} / ${reportFormatValue(final.stage_gate.total)}`,
          status: typeof final.stage_gate.all_ok === "boolean" ? (final.stage_gate.all_ok ? "pass" : "fail") : "",
          source: reportText(final.report_path),
        } : null,
        phaseOne ? { label: "Phase 1", value: "present", source: reportText(phaseOne.report_path) } : null,
        final ? { label: "Final", value: "present", source: reportText(final.report_path) } : null,
      ].filter(Boolean).map(reportNormalizeFact),
    },
    coverage: reportEmptySection(uiText("Chưa có coverage contract được chấp nhận trong report-summary.", "No accepted coverage contract is available in report-summary.")),
    quality: {
      state: metrics.length ? "partial" : "unavailable",
      message: metrics.length
        ? uiText("Giá trị được đọc nguyên trạng từ score report summary; App UI không tính lại metric.", "Values are read as reported by the score report summary; the App UI does not recalculate metrics.")
        : uiText("Chưa có metric được report-summary công bố.", "No metrics have been published by report-summary."),
      metrics,
    },
    comparison: comparison ? {
      state: gapRows.length ? "partial" : "unavailable",
      message: uiText("Chỉ hiển thị gap do report-summary công bố; App UI không tự tính delta.", "Only gaps published by report-summary are shown; the App UI does not calculate deltas."),
      baseline: reportText(comparison.baseline),
      candidate: reportText(comparison.candidate),
      metrics: gapRows,
    } : reportEmptySection(uiText("Report-summary chưa công bố so sánh nhiều nhánh.", "Report-summary has not published a multi-arm comparison.")),
    findings: {
      state: findings.length ? "partial" : "unavailable",
      message: findings.length
        ? uiText("Thuật ngữ đáng chú ý từ consistency summary; bằng chứng cấp lần xuất hiện chờ contract thuật ngữ.", "Notable terms come from the consistency summary; occurrence-level evidence awaits the terminology contract.")
        : uiText("Chưa có contract phát hiện từ pipeline thuật ngữ hoặc văn học.", "No findings contract is available from the terminology or literary pipeline."),
      items: findings,
    },
    execution: reportEmptySection(uiText("Contract Bằng chứng chạy chưa được Coordinator phát hành cho Báo cáo.", "The Execution Evidence contract has not been published by the Coordinator for Report.")),
    provenance: {
      state: artifacts.length ? "partial" : "unavailable",
      message: uiText("Chỉ các đường dẫn artifact được report-summary nêu rõ mới được hiển thị.", "Only artifact paths explicitly named by report-summary are displayed."),
      facts: artifacts.map((artifact, index) => reportNormalizeFact({
        id: `provenance-artifact-${index}`,
        label: artifact.label,
        value: artifact.path,
        source: artifact.path,
      }, index)),
    },
    artifacts: {
      state: artifacts.length ? "partial" : "unavailable",
      message: artifacts.length ? uiText("Đường dẫn artifact được relay từ report-summary.", "Artifact paths are relayed from report-summary.") : uiText("Chưa có đường dẫn artifact được báo cáo.", "No artifact path has been reported."),
      items: artifacts,
    },
    validationErrors: [],
  };
}

function reportNormalizeCanonical(raw, context) {
  const validationErrors = reportArray(raw.validation_errors || raw.errors).map(error => (
    reportText(reportIsObject(error) ? (error.message || error.code) : error)
  )).filter(Boolean);
  const contractStatus = validationErrors.length
    ? "invalid"
    : reportState(raw.contract_status || raw.status, "partial");
  const normalizeSection = (section, fallbackMessage) => {
    if (!reportIsObject(section)) return reportEmptySection(fallbackMessage);
    return {
      ...section,
      state: reportState(section.state || section.status, "partial"),
      message: reportText(section.message || section.note),
    };
  };
  const summary = normalizeSection(raw.summary, uiText("Producer chưa công bố Tổng quan.", "Summary has not been published by its producer."));
  const coverage = normalizeSection(raw.coverage, uiText("Producer chưa công bố Phạm vi.", "Coverage has not been published by its producer."));
  const quality = normalizeSection(raw.quality, uiText("Producer chưa công bố chỉ số Chất lượng.", "Quality metrics have not been published by their producer."));
  const comparison = normalizeSection(raw.comparison, uiText("Producer chưa công bố So sánh.", "Comparison has not been published by its producer."));
  const findings = normalizeSection(raw.findings, uiText("Producer chưa công bố Phát hiện.", "Findings have not been published by their producer."));
  const execution = normalizeSection(raw.execution_evidence || raw.execution, uiText("Producer chưa công bố Bằng chứng chạy.", "Execution Evidence has not been published by its producer."));
  const provenance = normalizeSection(raw.provenance, uiText("Producer chưa công bố Nguồn gốc.", "Provenance has not been published by its producer."));
  const artifacts = normalizeSection(raw.artifacts, uiText("Producer chưa công bố Artifacts.", "Artifacts have not been published by their producer."));

  summary.facts = reportArray(summary.facts).map(reportNormalizeFact);
  coverage.facts = reportArray(coverage.facts).map(reportNormalizeFact);
  quality.metrics = reportArray(quality.metrics).map(reportNormalizeMetric).filter(Boolean);
  comparison.metrics = reportArray(comparison.metrics).map(reportNormalizeComparisonMetric).filter(Boolean);
  findings.items = reportArray(findings.items).map(reportNormalizeFinding).filter(Boolean);
  execution.facts = reportArray(execution.facts).map(reportNormalizeFact);
  provenance.facts = reportArray(provenance.facts).map(reportNormalizeFact);
  artifacts.items = reportArray(artifacts.items).map(reportNormalizeArtifact).filter(Boolean);

  const verdictRaw = reportIsObject(summary.verdict) ? summary.verdict : null;
  summary.verdict = verdictRaw ? {
    state: reportText(verdictRaw.state, "not_issued").toLowerCase(),
    label: reportText(verdictRaw.label, uiText("CHƯA CÓ PHÁN QUYẾT", "NO VERDICT")),
    reasons: reportArray(verdictRaw.reasons).map(reason => reportText(reason)).filter(Boolean),
    source: reportText(verdictRaw.source || verdictRaw.artifact_path),
  } : { state: "not_issued", label: uiText("CHƯA CÓ PHÁN QUYẾT", "NO VERDICT"), reasons: [] };

  return {
    contractStatus,
    contractLabel: reportText(raw.contract_label, reportStatusMeta(contractStatus).label),
    statusMessage: reportText(raw.status_message, validationErrors.length
      ? uiText("Payload báo cáo không hợp lệ; UI đã dừng diễn giải các trường không xác minh.", "The report payload is invalid; the UI stopped interpreting unverified fields.")
      : uiText("Các phần chỉ phản ánh dữ liệu producer đã công bố.", "Sections reflect only data published by producers.")),
    schemaVersion: reportText(raw.schema_version, "not reported"),
    generatedAt: reportText(raw.generated_at),
    sourceLabel: reportText(raw.source_label, context.reportSource || "report contract"),
    summary,
    coverage,
    quality,
    comparison,
    findings,
    execution,
    provenance,
    artifacts,
    validationErrors,
  };
}

function reportNormalizeModel({ report, reportSource, runtimeAvailable, runId, selectedRun, projectId, projectTitle }) {
  const runStatus = reportText(selectedRun?.status);
  const context = { reportSource, runtimeAvailable, runId, runStatus, projectId, projectTitle };
  let normalized;

  if (!runtimeAvailable) {
    normalized = {
      contractStatus: "unavailable",
      contractLabel: "NO RUNTIME",
      statusMessage: uiText("Dự án này chưa được gắn với runtime job; chưa có báo cáo lần chạy để đọc.", "This project is not attached to a runtime job; there is no run report to read."),
      schemaVersion: "not available",
      generatedAt: "",
      sourceLabel: "none",
      summary: { ...reportEmptySection(uiText("Cấu hình pipeline và khởi tạo lần chạy để có báo cáo.", "Configure the pipeline and start a run to generate a report.")), verdict: { state: "not_issued", label: uiText("CHƯA CÓ PHÁN QUYẾT", "NO VERDICT"), reasons: [] }, facts: [] },
      coverage: reportEmptySection(uiText("Chưa có lần chạy.", "No run yet.")),
      quality: reportEmptySection(uiText("Chưa có lần chạy.", "No run yet.")),
      comparison: reportEmptySection(uiText("Chưa có lần chạy.", "No run yet.")),
      findings: reportEmptySection(uiText("Chưa có lần chạy.", "No run yet.")),
      execution: reportEmptySection(uiText("Chưa có lần chạy.", "No run yet.")),
      provenance: reportEmptySection(uiText("Chưa có lần chạy.", "No run yet.")),
      artifacts: reportEmptySection(uiText("Chưa có lần chạy.", "No run yet.")),
      validationErrors: [],
    };
  } else if (!runId) {
    normalized = {
      contractStatus: "unavailable",
      contractLabel: "SELECT RUN",
      statusMessage: uiText("Chọn một lần chạy để mở báo cáo tương ứng.", "Choose a run to open its report."),
      schemaVersion: "not selected",
      generatedAt: "",
      sourceLabel: "none",
      summary: { ...reportEmptySection(uiText("Chưa chọn lần chạy.", "No run selected.")), verdict: { state: "not_issued", label: uiText("CHƯA CÓ PHÁN QUYẾT", "NO VERDICT"), reasons: [] }, facts: [] },
      coverage: reportEmptySection(uiText("Chưa chọn lần chạy.", "No run selected.")),
      quality: reportEmptySection(uiText("Chưa chọn lần chạy.", "No run selected.")),
      comparison: reportEmptySection(uiText("Chưa chọn lần chạy.", "No run selected.")),
      findings: reportEmptySection(uiText("Chưa chọn lần chạy.", "No run selected.")),
      execution: reportEmptySection(uiText("Chưa chọn lần chạy.", "No run selected.")),
      provenance: reportEmptySection(uiText("Chưa chọn lần chạy.", "No run selected.")),
      artifacts: reportEmptySection(uiText("Chưa chọn lần chạy.", "No run selected.")),
      validationErrors: [],
    };
  } else if (report !== null && report !== undefined && !reportIsObject(report)) {
    normalized = reportNormalizeCanonical({
      contract_status: "invalid",
      validation_errors: ["Report payload must be an object."],
    }, context);
  } else if (!reportIsObject(report) || !Object.keys(report).length) {
    const state = reportTerminal(runStatus) ? "unavailable" : "pending";
    normalized = {
      contractStatus: state,
      contractLabel: state === "pending" ? "WAITING" : "NOT GENERATED",
      statusMessage: state === "pending"
        ? uiText("Lần chạy đang hoạt động hoặc chưa phát hành artifact báo cáo.", "The run is active or has not yet published a report artifact.")
        : uiText("Lần chạy không có payload báo cáo khả dụng trong read model hiện tại.", "The run has no report payload available in the current read model."),
      schemaVersion: "not reported",
      generatedAt: "",
      sourceLabel: reportSource || "none",
      summary: { state, message: uiText("Chưa có summary đã lưu.", "No persisted summary is available."), verdict: { state: "not_issued", label: uiText("CHƯA CÓ PHÁN QUYẾT", "NO VERDICT"), reasons: [] }, facts: [] },
      coverage: { state, message: uiText("Chờ coverage contract.", "Waiting for the coverage contract.") },
      quality: { state, message: uiText("Chờ metrics contract.", "Waiting for the metrics contract.") },
      comparison: { state, message: uiText("Chờ comparison contract.", "Waiting for the comparison contract.") },
      findings: { state, message: uiText("Chờ findings contract.", "Waiting for the findings contract.") },
      execution: { state, message: uiText("Chờ execution evidence contract.", "Waiting for the execution evidence contract.") },
      provenance: { state, message: uiText("Chờ provenance contract.", "Waiting for the provenance contract.") },
      artifacts: { state, message: uiText("Chờ artifact manifest.", "Waiting for the artifact manifest.") },
      validationErrors: [],
    };
  } else {
    normalized = reportSource === "report-summary"
      ? reportLegacySummary(report, context)
      : reportNormalizeCanonical(report, context);
  }

  return {
    ...normalized,
    identity: {
      runId: runId || "",
      projectId: projectId || "",
      projectTitle: projectTitle || "",
      runStatus,
      startedAt: reportText(selectedRun?.started_at),
      finishedAt: reportText(selectedRun?.finished_at || selectedRun?.ended_at),
    },
  };
}

function ReportState({ state = "unavailable", message, owner, compact = false }) {
  const meta = reportStatusMeta(state);
  return (
    <div className={`report-state report-tone-${meta.tone}${compact ? " compact" : ""}`}>
      <span className="report-state-glyph" aria-hidden="true">{meta.glyph}</span>
      <div>
        <strong>{meta.label}</strong>
        {message && <p>{message}</p>}
        {owner && <small>{uiText("Nguồn contract", "Contract source")}: {owner}</small>}
      </div>
    </div>
  );
}

function ReportSectionHeader({ definition, state }) {
  const meta = reportStatusMeta(state);
  return (
    <div className="report-section-head">
      <div>
        <span className="report-section-index">{definition.index}</span>
        <div>
          <span className="report-section-kicker">{reportSectionShortLabel(definition)}</span>
          <h2>{reportSectionLabel(definition)}</h2>
        </div>
      </div>
      <span className={`report-section-status report-tone-${meta.tone}`}>
        <span aria-hidden="true">{meta.glyph}</span>{meta.label}
      </span>
    </div>
  );
}

function ReportFactGrid({ facts = [] }) {
  if (!facts.length) return null;
  return (
    <div className="report-fact-grid">
      {facts.map((fact, index) => (
        <article className="report-fact" key={fact.id || `${fact.label}-${index}`}>
          <span>{fact.label}</span>
          <strong>{reportFormatValue(fact.value)}{fact.unit ? <em> {fact.unit}</em> : null}</strong>
          {fact.note && <p>{fact.note}</p>}
          {fact.source && <code title={fact.source}>{fact.source}</code>}
        </article>
      ))}
    </div>
  );
}

function ReportMetricGrid({ metrics = [] }) {
  if (!metrics.length) return null;
  return (
    <div className="report-metric-grid">
      {metrics.map((metric, index) => (
        <article className="report-metric" key={metric.id || `${metric.key}-${index}`}>
          <div className="report-metric-head">
            <span className="report-metric-code">{metric.key}</span>
            {metric.status && <span className={`report-mini-status status-${String(metric.status).toLowerCase()}`}>{metric.status}</span>}
          </div>
          <div className="report-metric-value">{reportFormatValue(metric.value)}</div>
          <div className="report-metric-unit">{metric.unit || uiText("chưa báo đơn vị", "unit not reported")}</div>
          <h3>{metric.label}</h3>
          <p>{metric.definition || uiText("Contract được chấp nhận chưa công bố định nghĩa metric này.", "The accepted contract does not publish a definition for this metric.")}</p>
          <dl>
            {metric.scope && <><dt>{uiText("Phạm vi", "Scope")}</dt><dd>{metric.scope}</dd></>}
            {metric.direction && <><dt>{uiText("Hướng", "Direction")}</dt><dd>{metric.direction}</dd></>}
            {metric.source && <><dt>{uiText("Nguồn", "Source")}</dt><dd><code>{metric.source}</code></dd></>}
          </dl>
        </article>
      ))}
    </div>
  );
}

function ReportComparison({ section }) {
  const metrics = reportArray(section.metrics);
  if (!["ready", "partial"].includes(section.state) || !metrics.length) {
    return <ReportState state={section.state} message={section.message} owner="Evaluation" />;
  }
  return (
    <>
      {section.message && <p className="report-section-note">{section.message}</p>}
      <div className="report-comparison-meta">
        <span><small>{uiText("Mốc gốc", "Baseline")}</small><strong>{section.baseline || uiText("chưa báo cáo", "not reported")}</strong></span>
        <span aria-hidden="true">→</span>
        <span><small>{uiText("Phương án", "Candidate")}</small><strong>{section.candidate || uiText("chưa báo cáo", "not reported")}</strong></span>
      </div>
      <div className="report-comparison-table" role="table" aria-label={uiText("So sánh các nhánh đã báo cáo", "Reported arm comparison")}>
        <div className="report-comparison-row head" role="row">
          <span role="columnheader">{uiText("Chỉ số", "Metric")}</span>
          <span role="columnheader">{section.baseline || uiText("Mốc gốc", "Baseline")}</span>
          <span role="columnheader">{section.candidate || uiText("Phương án", "Candidate")}</span>
          <span role="columnheader">{uiText("Chênh lệch đã báo cáo", "Reported delta")}</span>
        </div>
        {metrics.map((metric, index) => (
          <div className="report-comparison-row" role="row" key={metric.id || `${metric.key}-${index}`}>
            <span role="cell"><b>{metric.key}</b><small>{metric.label}</small></span>
            <span role="cell">{reportFormatValue(metric.baseline)}</span>
            <span role="cell">{reportFormatValue(metric.candidate)}</span>
            <span role="cell" className={metric.status ? `status-${String(metric.status).toLowerCase()}` : ""}>
              {reportFormatValue(metric.delta)}{metric.unit ? <small>{metric.unit}</small> : null}
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

function ReportEvidenceValue({ value }) {
  if (value === null || value === undefined || value === "") return <span>—</span>;
  if (reportIsObject(value) || Array.isArray(value)) {
    return <pre>{JSON.stringify(value, null, 2)}</pre>;
  }
  return <p>{String(value)}</p>;
}

function ReportFindings({ section }) {
  const items = reportArray(section.items);
  const [selectedId, setSelectedId] = React.useState(items[0]?.id || "");
  const signature = items.map(item => item.id).join("|");
  React.useEffect(() => {
    if (!items.length) {
      setSelectedId("");
      return;
    }
    if (!items.some(item => item.id === selectedId)) setSelectedId(items[0].id);
  }, [signature, selectedId]);
  if (!["ready", "partial"].includes(section.state)) {
    return <ReportState state={section.state} message={section.message} owner="Terminology + Literary" />;
  }
  if (!items.length) {
    return <ReportState state="empty" message={section.message || uiText("Producer xác nhận không có phát hiện trong phạm vi báo cáo.", "The producer confirmed that there are no findings in the report scope.")} owner="Terminology + Literary" />;
  }
  const selected = items.find(item => item.id === selectedId) || items[0];
  return (
    <>
      {section.message && <p className="report-section-note">{section.message}</p>}
      <div className="report-findings-layout">
        <div className="report-findings-list" role="list" aria-label={uiText("Các phát hiện đã báo cáo", "Reported findings")}>
          {items.map(item => (
            <button
              type="button"
              role="listitem"
              className={`report-finding-row${selected?.id === item.id ? " active" : ""}`}
              key={item.id}
              onClick={() => setSelectedId(item.id)}
              aria-pressed={selected?.id === item.id}
            >
              <span className={`report-finding-severity severity-${item.severity}`} aria-hidden="true" />
              <span>
                <small>{item.category}{item.location ? ` · ${item.location}` : ""}</small>
                <strong>{item.title}</strong>
                <em>{item.summary || uiText("Không có tóm tắt được báo cáo.", "No summary reported.")}</em>
              </span>
            </button>
          ))}
        </div>
        <aside className="report-finding-detail" aria-label={uiText("Chi tiết phát hiện", "Finding detail")}>
          <div className="report-finding-detail-head">
            <span>{selected.category}</span>
            <code>{selected.id}</code>
          </div>
          <h3>{selected.title}</h3>
          {selected.summary && <p>{selected.summary}</p>}
          <dl>
            {selected.location && <><dt>{uiText("Vị trí", "Location")}</dt><dd>{selected.location}</dd></>}
            {selected.owner && <><dt>{uiText("Phụ trách", "Owner")}</dt><dd>{selected.owner}</dd></>}
            {selected.artifactPath && <><dt>{uiText("Tệp đầu ra", "Artifact")}</dt><dd><code>{selected.artifactPath}</code></dd></>}
          </dl>
          <div className="report-evidence-block">
            <span>{uiText("Bằng chứng đã lưu", "Persisted evidence")}</span>
            <ReportEvidenceValue value={selected.evidence} />
          </div>
          {selected.meta && (
            <div className="report-evidence-block">
              <span>{uiText("Metadata đã báo cáo", "Reported metadata")}</span>
              <ReportEvidenceValue value={selected.meta} />
            </div>
          )}
        </aside>
      </div>
    </>
  );
}

function ReportArtifacts({ section }) {
  const items = reportArray(section.items);
  if (!["ready", "partial"].includes(section.state) || !items.length) {
    return <ReportState state={section.state} message={section.message} owner="All producers" />;
  }
  return (
    <>
      {section.message && <p className="report-section-note">{section.message}</p>}
      <div className="report-artifact-list">
        {items.map((artifact, index) => (
          <article key={artifact.id || `${artifact.path}-${index}`}>
            <span className="report-artifact-kind">{artifact.kind}</span>
            <div>
              <strong>{artifact.label}</strong>
              <code>{artifact.path || uiText("chưa báo đường dẫn", "path not reported")}</code>
              {artifact.note && <p>{artifact.note}</p>}
            </div>
            <span className="report-artifact-status">{artifact.status}</span>
            {artifact.digest && <code className="report-artifact-digest">{artifact.digest}</code>}
          </article>
        ))}
      </div>
    </>
  );
}

function AgentReportView(props) {
  const {
    runId = "",
    runs = [],
    selectedRun = null,
    onSelectRun,
    report = null,
    reportSource = "",
    runtimeAvailable = true,
    projectId = "",
    projectTitle = "",
    onBack,
    onOpenConsole,
    onOpenStoryBible,
    onRefresh,
    theme = "light",
    onToggleTheme,
    fixtureOnly = false,
  } = props;
  const [uiLocale, setUiLocale] = useThesisLocale();
  const scrollRef = React.useRef(null);
  const [activeSection, setActiveSection] = React.useState("summary");
  const model = React.useMemo(() => reportNormalizeModel({
    report,
    reportSource,
    runtimeAvailable,
    runId,
    selectedRun,
    projectId,
    projectTitle,
  }), [report, reportSource, runtimeAvailable, runId, selectedRun, projectId, projectTitle, uiLocale]);
  const overallMeta = reportStatusMeta(model.contractStatus);

  React.useEffect(() => {
    const root = scrollRef.current;
    if (!root || typeof IntersectionObserver === "undefined") return undefined;
    const nodes = REPORT_SECTION_DEFS.map(section => root.querySelector(`#report-${section.id}`)).filter(Boolean);
    const observer = new IntersectionObserver(entries => {
      const visible = entries
        .filter(entry => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (visible?.target?.dataset?.reportSection) setActiveSection(visible.target.dataset.reportSection);
    }, { root, rootMargin: "-12% 0px -72% 0px", threshold: [0, 0.08, 0.25] });
    nodes.forEach(node => observer.observe(node));
    return () => observer.disconnect();
  }, [runId, model.contractStatus]);

  function scrollToSection(id) {
    const node = scrollRef.current?.querySelector(`#report-${id}`);
    if (!node) return;
    setActiveSection(id);
    node.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  const sectionMap = {
    summary: model.summary,
    coverage: model.coverage,
    quality: model.quality,
    comparison: model.comparison,
    findings: model.findings,
    execution: model.execution,
    provenance: model.provenance,
    artifacts: model.artifacts,
  };
  const identityFacts = [
    { id: "identity-run", label: uiText("ID lần chạy", "Run ID"), value: model.identity.runId || uiText("chưa chọn", "not selected") },
    { id: "identity-project", label: uiText("Dự án", "Project"), value: model.identity.projectId || uiText("không khả dụng", "not available") },
    { id: "identity-state", label: uiText("Trạng thái chạy", "Run state"), value: model.identity.runStatus || uiText("chưa báo cáo", "not reported") },
    { id: "identity-generated", label: uiText("Thời điểm tạo báo cáo", "Report generated"), value: reportFormatTime(model.generatedAt) },
  ].map(reportNormalizeFact);

  return (
    <div className={`agentreport report-theme-${theme === "dark" ? "dark" : "light"}`}>
      <header className="report-header">
        {onBack && <button className="report-btn report-back" type="button" onClick={onBack}>&larr; {uiText("Workspace", "Workspace")}</button>}
        <span className="report-brand">⬢ {uiText("BÁO CÁO LẦN CHẠY", "RUN REPORT")}</span>
        <nav className="run-surface-tabs" aria-label={uiText("Các chế độ lần chạy", "Run views")}>
          {onOpenConsole && <button className="run-surface-tab" type="button" onClick={onOpenConsole}>Console</button>}
          <span className="run-surface-tab active" aria-current="page">{uiText("Báo cáo", "Report")}</span>
          {onOpenStoryBible && <button className="run-surface-tab" type="button" onClick={onOpenStoryBible}>{uiText("Bộ hồ sơ", "Story Bible")}</button>}
        </nav>
        {projectId && <span className="report-project" title={projectId}>{projectId}</span>}
        <select className="report-run-picker" aria-label={uiText("Chọn lần chạy", "Run picker")} value={runId || ""} onChange={event => onSelectRun?.(event.target.value)}>
          {!runId && <option value="">{uiText("Chọn lần chạy", "Choose run")}</option>}
          {runs.slice(0, 40).map(run => (
            <option key={run.run_id} value={run.run_id}>{run.run_id}{run.status ? ` · ${run.status}` : ""}</option>
          ))}
        </select>
        <div className="report-header-actions">
          {fixtureOnly && <span className="report-fixture-chip">FIXTURE ONLY</span>}
          <ThesisLocaleSwitch locale={uiLocale} onChange={setUiLocale} compact />
          {onRefresh && <button className="report-btn" type="button" onClick={onRefresh}>↻ {uiText("Làm mới", "Refresh")}</button>}
          {onToggleTheme && <button className="report-btn" type="button" onClick={onToggleTheme}>◐ {uiText("Giao diện", "Theme")}</button>}
          <span className={`report-contract-chip report-tone-${overallMeta.tone}`}>
            <span aria-hidden="true">{overallMeta.glyph}</span>{model.contractLabel || overallMeta.label}
          </span>
        </div>
      </header>

      {fixtureOnly && (
        <div className="report-fixture-banner" role="note">
          {uiText("FIXTURE-ONLY DEV HARNESS · Mọi giá trị trên trang này chỉ dùng kiểm tra UI, không phải kết quả pipeline.", "FIXTURE-ONLY DEV HARNESS · All values on this page are for UI testing only and are not pipeline results.")}
        </div>
      )}

      <div className="report-body">
        <aside className="report-nav" aria-label={uiText("Mục lục báo cáo", "Report contents")}>
          <div className="report-nav-status">
            <span>{uiText("TRẠNG THÁI BÁO CÁO", "REPORT STATUS")}</span>
            <strong className={`report-tone-${overallMeta.tone}`}><i aria-hidden="true">{overallMeta.glyph}</i>{model.contractLabel || overallMeta.label}</strong>
            <p>{model.statusMessage}</p>
          </div>
          <nav>
            {REPORT_SECTION_DEFS.map(section => {
              const state = sectionMap[section.id]?.state || "unavailable";
              const meta = reportStatusMeta(state);
              return (
                <button
                  type="button"
                  key={section.id}
                  className={activeSection === section.id ? "active" : ""}
                  onClick={() => scrollToSection(section.id)}
                  aria-current={activeSection === section.id ? "location" : undefined}
                >
                  <span>{section.index}</span>
                  <b>{reportSectionLabel(section)}</b>
                  <i className={`report-tone-${meta.tone}`} aria-label={meta.label}>{meta.glyph}</i>
                </button>
              );
            })}
          </nav>
          <div className="report-nav-note">
            <span>READ MODEL</span>
            <code>{model.sourceLabel}</code>
            <p>{uiText("Snapshot cuối đã lưu · không đi theo replay cursor.", "Final persisted snapshot · does not follow the replay cursor.")}</p>
          </div>
        </aside>

        <main className="report-scroll" ref={scrollRef} tabIndex="0" aria-label={uiText("Nội dung Báo cáo lần chạy đầy đủ", "Full Run Report content")}>
          <div className={`report-status-banner report-tone-${overallMeta.tone}`} role="status">
            <span aria-hidden="true">{overallMeta.glyph}</span>
            <div>
              <strong>{model.contractLabel || overallMeta.label}</strong>
              <p>{model.statusMessage}</p>
            </div>
            <code>{model.schemaVersion}</code>
          </div>
          {model.validationErrors.length > 0 && (
            <div className="report-validation-errors" role="alert">
              <strong>{uiText("Kiểm tra contract thất bại", "Contract validation failed")}</strong>
              <ul>{model.validationErrors.map((error, index) => <li key={`${error}-${index}`}>{error}</li>)}</ul>
            </div>
          )}

          <section id="report-summary" data-report-section="summary" className="report-section report-summary-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[0]} state={model.summary.state} />
            <div className="report-hero">
              <div>
                <span className="report-eyebrow">{uiText("BÁO CÁO ĐẦY ĐỦ", "FULL RUN REPORT")}</span>
                <h1>{model.summary.title || projectTitle || uiText("Báo cáo kết quả lần chạy", "Run results report")}</h1>
                <p>{model.summary.description || uiText("Bản đọc tổng hợp các fact và bằng chứng đã được producer lưu.", "A consolidated reading of facts and evidence persisted by producers.")}</p>
              </div>
              <div className={`report-verdict verdict-${model.summary.verdict?.state || "not_issued"}`}>
                <span>{uiText("PHÁN QUYẾT", "VERDICT")}</span>
                <strong>{model.summary.verdict?.label || uiText("CHƯA CÓ PHÁN QUYẾT", "NO VERDICT")}</strong>
                {model.summary.verdict?.source && <code>{model.summary.verdict.source}</code>}
              </div>
            </div>
            {model.summary.verdict?.reasons?.length > 0 && (
              <ul className="report-reasons">{model.summary.verdict.reasons.map((reason, index) => <li key={`${reason}-${index}`}>{reason}</li>)}</ul>
            )}
            <ReportFactGrid facts={identityFacts} />
            <ReportFactGrid facts={reportArray(model.summary.facts)} />
            {!reportArray(model.summary.facts).length && !["ready", "partial"].includes(model.summary.state) && (
              <ReportState state={model.summary.state} message={model.summary.message} owner={REPORT_SECTION_DEFS[0].owner} />
            )}
          </section>

          <section id="report-coverage" data-report-section="coverage" className="report-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[1]} state={model.coverage.state} />
            {reportArray(model.coverage.facts).length
              ? <>{model.coverage.message && <p className="report-section-note">{model.coverage.message}</p>}<ReportFactGrid facts={model.coverage.facts} /></>
              : <ReportState state={model.coverage.state} message={model.coverage.message} owner={REPORT_SECTION_DEFS[1].owner} />}
          </section>

          <section id="report-quality" data-report-section="quality" className="report-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[2]} state={model.quality.state} />
            {reportArray(model.quality.metrics).length
              ? <>{model.quality.message && <p className="report-section-note">{model.quality.message}</p>}<ReportMetricGrid metrics={model.quality.metrics} /></>
              : <ReportState state={model.quality.state} message={model.quality.message} owner={REPORT_SECTION_DEFS[2].owner} />}
          </section>

          <section id="report-comparison" data-report-section="comparison" className="report-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[3]} state={model.comparison.state} />
            <ReportComparison section={model.comparison} />
          </section>

          <section id="report-findings" data-report-section="findings" className="report-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[4]} state={model.findings.state} />
            <ReportFindings section={model.findings} />
          </section>

          <section id="report-execution" data-report-section="execution" className="report-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[5]} state={model.execution.state} />
            {reportArray(model.execution.facts).length
              ? <>{model.execution.message && <p className="report-section-note">{model.execution.message}</p>}<ReportFactGrid facts={model.execution.facts} /></>
              : <ReportState state={model.execution.state} message={model.execution.message} owner={REPORT_SECTION_DEFS[5].owner} />}
          </section>

          <section id="report-provenance" data-report-section="provenance" className="report-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[6]} state={model.provenance.state} />
            {reportArray(model.provenance.facts).length
              ? <>{model.provenance.message && <p className="report-section-note">{model.provenance.message}</p>}<ReportFactGrid facts={model.provenance.facts} /></>
              : <ReportState state={model.provenance.state} message={model.provenance.message} owner={REPORT_SECTION_DEFS[6].owner} />}
          </section>

          <section id="report-artifacts" data-report-section="artifacts" className="report-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[7]} state={model.artifacts.state} />
            <ReportArtifacts section={model.artifacts} />
          </section>

          <footer className="report-footer">
            <span>{uiText("BÁO CÁO LẦN CHẠY · CHỈ ĐỌC", "RUN REPORT · READ ONLY")}</span>
            <p>{uiText("UI không tính metric, không sửa frozen memory và không suy diễn phán quyết khi thiếu contract.", "The UI does not calculate metrics, modify frozen memory, or infer a verdict when the contract is missing.")}</p>
          </footer>
        </main>
      </div>
    </div>
  );
}

window.AgentReportView = AgentReportView;
