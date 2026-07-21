/* Run Report shell — read-only presentation surface for persisted run facts.
   Production currently receives the existing report-summary read model only.
   Missing sections remain explicitly unavailable until their owning pipeline
   publishes an accepted contract; this component never derives new metrics. */

const REPORT_SECTION_DEFS = Object.freeze([
  { id: "summary", index: "01", label: "Tổng quan", shortLabel: "Summary", owner: "Coordinator + Evaluation" },
  { id: "coverage", index: "02", label: "Phạm vi", shortLabel: "Coverage", owner: "Input Normalization" },
  { id: "quality", index: "03", label: "Chất lượng", shortLabel: "Quality", owner: "Evaluation + domain pipelines" },
  { id: "comparison", index: "04", label: "So sánh", shortLabel: "Comparison", owner: "Evaluation" },
  { id: "findings", index: "05", label: "Phát hiện", shortLabel: "Findings", owner: "Terminology + Literary" },
  { id: "execution", index: "06", label: "Bằng chứng chạy", shortLabel: "Execution", owner: "Coordinator" },
  { id: "provenance", index: "07", label: "Nguồn gốc", shortLabel: "Provenance", owner: "Input Normalization + Coordinator" },
  { id: "artifacts", index: "08", label: "Artifacts", shortLabel: "Artifacts", owner: "All producers" },
]);

const REPORT_TERMINAL_STATUSES = new Set(["done", "failed", "cancelled", "canceled", "error"]);
const REPORT_ALLOWED_STATES = new Set(["ready", "partial", "pending", "unavailable", "invalid", "one_arm", "empty"]);
const REPORT_STATUS_META = Object.freeze({
  ready: { label: "READY", tone: "good", glyph: "●" },
  partial: { label: "PARTIAL", tone: "warn", glyph: "◐" },
  pending: { label: "PENDING", tone: "info", glyph: "◌" },
  unavailable: { label: "UNAVAILABLE", tone: "muted", glyph: "—" },
  invalid: { label: "INVALID", tone: "bad", glyph: "×" },
  one_arm: { label: "ONE ARM", tone: "info", glyph: "Ⅰ" },
  empty: { label: "EMPTY", tone: "muted", glyph: "○" },
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
  return REPORT_STATUS_META[reportState(state)] || REPORT_STATUS_META.unavailable;
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
  return parsed.toLocaleString("vi-VN", {
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
      ? "Đang hiển thị report-summary đã persisted. Full Run Report contract chưa được nối vào App UI."
      : reportTerminal(context.runStatus)
        ? "Run đã kết thúc nhưng report-summary hiện không có dữ liệu khả dụng."
        : "Run chưa phát hành report-summary; trang sẽ giữ trạng thái chờ.",
    schemaVersion: "report-summary (legacy read model)",
    generatedAt: "",
    sourceLabel: "report-summary · read-only",
    summary: {
      state: hasPayload ? "partial" : "pending",
      message: hasPayload ? "Tổng quan giới hạn trong dữ liệu summary hiện có." : "Chờ report-summary.",
      verdict,
      title: "Kết quả run",
      description: "Snapshot báo cáo cuối đã persisted; không đồng bộ theo replay cursor của Console.",
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
    coverage: reportEmptySection("Chưa có coverage contract được chấp nhận trong report-summary."),
    quality: {
      state: metrics.length ? "partial" : "unavailable",
      message: metrics.length
        ? "Giá trị được đọc nguyên trạng từ score report summary; App UI không tính lại metric."
        : "Chưa có metric được report-summary công bố.",
      metrics,
    },
    comparison: comparison ? {
      state: gapRows.length ? "partial" : "unavailable",
      message: "Chỉ hiển thị gap do report-summary công bố; App UI không tự tính delta.",
      baseline: reportText(comparison.baseline),
      candidate: reportText(comparison.candidate),
      metrics: gapRows,
    } : reportEmptySection("Report-summary chưa công bố so sánh nhiều arm."),
    findings: {
      state: findings.length ? "partial" : "unavailable",
      message: findings.length
        ? "Notable terms từ consistency summary; bằng chứng occurrence-level chờ contract thuật ngữ."
        : "Chưa có findings contract từ pipeline thuật ngữ hoặc văn học.",
      items: findings,
    },
    execution: reportEmptySection("Execution Evidence contract chưa được Coordinator phát hành cho Report."),
    provenance: {
      state: artifacts.length ? "partial" : "unavailable",
      message: "Chỉ các đường dẫn artifact được report-summary nêu rõ mới được hiển thị.",
      facts: artifacts.map((artifact, index) => reportNormalizeFact({
        id: `provenance-artifact-${index}`,
        label: artifact.label,
        value: artifact.path,
        source: artifact.path,
      }, index)),
    },
    artifacts: {
      state: artifacts.length ? "partial" : "unavailable",
      message: artifacts.length ? "Artifact paths được relay từ report-summary." : "Chưa có artifact path được report.",
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
  const summary = normalizeSection(raw.summary, "Summary chưa được producer công bố.");
  const coverage = normalizeSection(raw.coverage, "Coverage chưa được producer công bố.");
  const quality = normalizeSection(raw.quality, "Quality metrics chưa được producer công bố.");
  const comparison = normalizeSection(raw.comparison, "Comparison chưa được producer công bố.");
  const findings = normalizeSection(raw.findings, "Findings chưa được producer công bố.");
  const execution = normalizeSection(raw.execution_evidence || raw.execution, "Execution Evidence chưa được producer công bố.");
  const provenance = normalizeSection(raw.provenance, "Provenance chưa được producer công bố.");
  const artifacts = normalizeSection(raw.artifacts, "Artifacts chưa được producer công bố.");

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
    label: reportText(verdictRaw.label, "CHƯA CÓ PHÁN QUYẾT"),
    reasons: reportArray(verdictRaw.reasons).map(reason => reportText(reason)).filter(Boolean),
    source: reportText(verdictRaw.source || verdictRaw.artifact_path),
  } : { state: "not_issued", label: "CHƯA CÓ PHÁN QUYẾT", reasons: [] };

  return {
    contractStatus,
    contractLabel: reportText(raw.contract_label, reportStatusMeta(contractStatus).label),
    statusMessage: reportText(raw.status_message, validationErrors.length
      ? "Report payload không hợp lệ; UI đã dừng diễn giải các trường không xác minh."
      : "Các section chỉ phản ánh dữ liệu producer đã công bố."),
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
      statusMessage: "Project này chưa được gắn với runtime job; chưa có run report để đọc.",
      schemaVersion: "not available",
      generatedAt: "",
      sourceLabel: "none",
      summary: { ...reportEmptySection("Cấu hình pipeline và khởi tạo run để có báo cáo."), verdict: { state: "not_issued", label: "CHƯA CÓ PHÁN QUYẾT", reasons: [] }, facts: [] },
      coverage: reportEmptySection("Chưa có run."),
      quality: reportEmptySection("Chưa có run."),
      comparison: reportEmptySection("Chưa có run."),
      findings: reportEmptySection("Chưa có run."),
      execution: reportEmptySection("Chưa có run."),
      provenance: reportEmptySection("Chưa có run."),
      artifacts: reportEmptySection("Chưa có run."),
      validationErrors: [],
    };
  } else if (!runId) {
    normalized = {
      contractStatus: "unavailable",
      contractLabel: "SELECT RUN",
      statusMessage: "Chọn một run để mở báo cáo tương ứng.",
      schemaVersion: "not selected",
      generatedAt: "",
      sourceLabel: "none",
      summary: { ...reportEmptySection("Chưa chọn run."), verdict: { state: "not_issued", label: "CHƯA CÓ PHÁN QUYẾT", reasons: [] }, facts: [] },
      coverage: reportEmptySection("Chưa chọn run."),
      quality: reportEmptySection("Chưa chọn run."),
      comparison: reportEmptySection("Chưa chọn run."),
      findings: reportEmptySection("Chưa chọn run."),
      execution: reportEmptySection("Chưa chọn run."),
      provenance: reportEmptySection("Chưa chọn run."),
      artifacts: reportEmptySection("Chưa chọn run."),
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
        ? "Run đang hoạt động hoặc chưa phát hành report artifact."
        : "Run không có report payload khả dụng ở read model hiện tại.",
      schemaVersion: "not reported",
      generatedAt: "",
      sourceLabel: reportSource || "none",
      summary: { state, message: "Chưa có summary persisted.", verdict: { state: "not_issued", label: "CHƯA CÓ PHÁN QUYẾT", reasons: [] }, facts: [] },
      coverage: { state, message: "Chờ coverage contract." },
      quality: { state, message: "Chờ metrics contract." },
      comparison: { state, message: "Chờ comparison contract." },
      findings: { state, message: "Chờ findings contract." },
      execution: { state, message: "Chờ execution evidence contract." },
      provenance: { state, message: "Chờ provenance contract." },
      artifacts: { state, message: "Chờ artifact manifest." },
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
        {owner && <small>Nguồn contract: {owner}</small>}
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
          <span className="report-section-kicker">{definition.shortLabel}</span>
          <h2>{definition.label}</h2>
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
          <div className="report-metric-unit">{metric.unit || "unit not reported"}</div>
          <h3>{metric.label}</h3>
          <p>{metric.definition || "Contract được chấp nhận chưa công bố định nghĩa metric này."}</p>
          <dl>
            {metric.scope && <><dt>Scope</dt><dd>{metric.scope}</dd></>}
            {metric.direction && <><dt>Direction</dt><dd>{metric.direction}</dd></>}
            {metric.source && <><dt>Source</dt><dd><code>{metric.source}</code></dd></>}
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
        <span><small>Baseline</small><strong>{section.baseline || "not reported"}</strong></span>
        <span aria-hidden="true">→</span>
        <span><small>Candidate</small><strong>{section.candidate || "not reported"}</strong></span>
      </div>
      <div className="report-comparison-table" role="table" aria-label="Reported arm comparison">
        <div className="report-comparison-row head" role="row">
          <span role="columnheader">Metric</span>
          <span role="columnheader">{section.baseline || "Baseline"}</span>
          <span role="columnheader">{section.candidate || "Candidate"}</span>
          <span role="columnheader">Reported delta</span>
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
    return <ReportState state="empty" message={section.message || "Producer xác nhận không có finding trong scope của report."} owner="Terminology + Literary" />;
  }
  const selected = items.find(item => item.id === selectedId) || items[0];
  return (
    <>
      {section.message && <p className="report-section-note">{section.message}</p>}
      <div className="report-findings-layout">
        <div className="report-findings-list" role="list" aria-label="Reported findings">
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
                <em>{item.summary || "No summary reported."}</em>
              </span>
            </button>
          ))}
        </div>
        <aside className="report-finding-detail" aria-label="Finding detail">
          <div className="report-finding-detail-head">
            <span>{selected.category}</span>
            <code>{selected.id}</code>
          </div>
          <h3>{selected.title}</h3>
          {selected.summary && <p>{selected.summary}</p>}
          <dl>
            {selected.location && <><dt>Location</dt><dd>{selected.location}</dd></>}
            {selected.owner && <><dt>Owner</dt><dd>{selected.owner}</dd></>}
            {selected.artifactPath && <><dt>Artifact</dt><dd><code>{selected.artifactPath}</code></dd></>}
          </dl>
          <div className="report-evidence-block">
            <span>Persisted evidence</span>
            <ReportEvidenceValue value={selected.evidence} />
          </div>
          {selected.meta && (
            <div className="report-evidence-block">
              <span>Reported metadata</span>
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
              <code>{artifact.path || "path not reported"}</code>
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
    onRefresh,
    theme = "light",
    onToggleTheme,
    fixtureOnly = false,
  } = props;
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
  }), [report, reportSource, runtimeAvailable, runId, selectedRun, projectId, projectTitle]);
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
    { id: "identity-run", label: "Run ID", value: model.identity.runId || "not selected" },
    { id: "identity-project", label: "Project", value: model.identity.projectId || "not available" },
    { id: "identity-state", label: "Run state", value: model.identity.runStatus || "not reported" },
    { id: "identity-generated", label: "Report generated", value: reportFormatTime(model.generatedAt) },
  ].map(reportNormalizeFact);

  return (
    <div className={`agentreport report-theme-${theme === "dark" ? "dark" : "light"}`}>
      <header className="report-header">
        {onBack && <button className="report-btn report-back" type="button" onClick={onBack}>&larr; Workspace</button>}
        <span className="report-brand">⬢ RUN REPORT</span>
        <nav className="run-surface-tabs" aria-label="Run views">
          {onOpenConsole && <button className="run-surface-tab" type="button" onClick={onOpenConsole}>Console</button>}
          <span className="run-surface-tab active" aria-current="page">Report / Báo cáo</span>
        </nav>
        {projectId && <span className="report-project" title={projectId}>{projectId}</span>}
        <select className="report-run-picker" aria-label="Run picker" value={runId || ""} onChange={event => onSelectRun?.(event.target.value)}>
          {!runId && <option value="">Chọn run</option>}
          {runs.slice(0, 40).map(run => (
            <option key={run.run_id} value={run.run_id}>{run.run_id}{run.status ? ` · ${run.status}` : ""}</option>
          ))}
        </select>
        <div className="report-header-actions">
          {fixtureOnly && <span className="report-fixture-chip">FIXTURE ONLY</span>}
          {onRefresh && <button className="report-btn" type="button" onClick={onRefresh}>↻ Làm mới</button>}
          {onToggleTheme && <button className="report-btn" type="button" onClick={onToggleTheme}>◐ Giao diện</button>}
          <span className={`report-contract-chip report-tone-${overallMeta.tone}`}>
            <span aria-hidden="true">{overallMeta.glyph}</span>{model.contractLabel || overallMeta.label}
          </span>
        </div>
      </header>

      {fixtureOnly && (
        <div className="report-fixture-banner" role="note">
          FIXTURE-ONLY DEV HARNESS · Mọi giá trị trên trang này chỉ dùng kiểm tra UI, không phải kết quả pipeline.
        </div>
      )}

      <div className="report-body">
        <aside className="report-nav" aria-label="Mục lục báo cáo">
          <div className="report-nav-status">
            <span>REPORT STATUS</span>
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
                  <b>{section.label}</b>
                  <i className={`report-tone-${meta.tone}`} aria-label={meta.label}>{meta.glyph}</i>
                </button>
              );
            })}
          </nav>
          <div className="report-nav-note">
            <span>READ MODEL</span>
            <code>{model.sourceLabel}</code>
            <p>Final persisted snapshot · không đi theo replay cursor.</p>
          </div>
        </aside>

        <main className="report-scroll" ref={scrollRef} tabIndex="0" aria-label="Nội dung Full Run Report">
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
              <strong>Contract validation failed</strong>
              <ul>{model.validationErrors.map((error, index) => <li key={`${error}-${index}`}>{error}</li>)}</ul>
            </div>
          )}

          <section id="report-summary" data-report-section="summary" className="report-section report-summary-section">
            <ReportSectionHeader definition={REPORT_SECTION_DEFS[0]} state={model.summary.state} />
            <div className="report-hero">
              <div>
                <span className="report-eyebrow">FULL RUN REPORT</span>
                <h1>{model.summary.title || projectTitle || "Báo cáo kết quả run"}</h1>
                <p>{model.summary.description || "Bản đọc tổng hợp các facts và evidence đã được producer persist."}</p>
              </div>
              <div className={`report-verdict verdict-${model.summary.verdict?.state || "not_issued"}`}>
                <span>VERDICT</span>
                <strong>{model.summary.verdict?.label || "CHƯA CÓ PHÁN QUYẾT"}</strong>
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
            <span>RUN REPORT · READ ONLY</span>
            <p>UI không tính metric, không sửa frozen memory và không suy diễn verdict khi contract thiếu.</p>
          </footer>
        </main>
      </div>
    </div>
  );
}

window.AgentReportView = AgentReportView;
