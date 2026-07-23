/* Agent Console view — self-contained, no dependency on the rest of the prototype.
   Skin ported from app/prototype/design/agent_console_mock.html (Claude Design),
   scoped under .agentconsole (see console.css). Driven by the REAL one-button
   event vocabulary (orchestrator + translator window lifecycle), rendered generically
   by severity so any future event type still shows up truthfully.

   Used two ways:
     - dev harness console_dev.html mounts <AgentConsoleView> with golden-fixture data
     - the main app's AgentConsole wrapper (parts_center.jsx) adapts runControl -> props */

const CONSOLE_STAGE_PLAN = [
  { id: "preflight_check", label: "preflight", phase: 1 },
  { id: "builder_c2", label: "builder", phase: 1 },
  { id: "auditor_c3", label: "auditor", phase: 1 },
  { id: "decollision_c35", label: "decollision", phase: 1 },
  { id: "reelection_watchlist", label: "reelection", phase: 1 },
  { id: "translator", label: "translator", phase: 1 },
  { id: "score_run_phase_1", label: "score · phase 1", phase: 1, phaseEnd: true },
  { id: "cascade", label: "cascade", phase: 2 },
  { id: "sf_qe", label: "sf_qe", phase: 2 },
  { id: "sf_bt", label: "sf_bt", phase: 2 },
  { id: "pj", label: "pj", phase: 2, optional: true },
  { id: "score_run_final", label: "score · final", phase: 2 },
];

const CONSOLE_SEVERITY_GLYPH = { info: "├", warning: "▲", error: "✕", context: "⊙" };
const CONSOLE_TERMINAL_STATUSES = new Set(["done", "succeeded", "failed", "cancelled", "canceled", "error"]);
const CONSOLE_RENDER_CAP = 2000;
const CONSOLE_REPLAY_SPEEDS = [0.25, 0.5, 1, 2, 4];
const CONSOLE_REPLAY_EVENT_STEP_MS = 260;
const CONSOLE_REPLAY_MIN_GAP_MS = 80;
const CONSOLE_REPLAY_MAX_GAP_MS = 2000;
const CONSOLE_HEARTBEAT_MODES = new Set(["grouped", "hidden", "raw"]);
const CONSOLE_INJECTED_TIERS = ["hard", "soft", "preserve"];
const CONSOLE_TIER_LABELS = {
  hard: "mandatory (hard)",
  soft: "soft",
  preserve: "preserve",
};
const CONSOLE_SYSTEM_ORDER = ["openai_key", "gemini_key", "lmstudio_gemma", "lmstudio_bge", "cometkiwi_import"];
const CONSOLE_SYSTEM_LABELS = {
  openai_key: "OpenAI",
  gemini_key: "Gemini",
  lmstudio_gemma: "LM Studio · Gemma",
  lmstudio_bge: "LM Studio · BGE",
  cometkiwi_import: "COMETKiwi",
};
const CONSOLE_MEMORY_COLLECTIONS = new Set(["term", "entity"]);
const CONSOLE_MEMORY_OPERATIONS = new Set(["added", "reinforced", "revised"]);
const CONSOLE_MEMORY_LIFECYCLES = new Set(["committed"]);
const CONSOLE_MEMORY_DOMAIN_COLLECTIONS = {
  terminology: new Set(["term"]),
  literary: new Set(["term", "entity"]),
};
const CONSOLE_MEMORY_COLLECTION_LABELS = {
  term: "Terms",
  entity: "Entities",
};
const CONSOLE_MEMORY_DOMAIN_LABELS = {
  terminology: "D2L",
  literary: "Literary",
};
const CONSOLE_MEMORY_OPERATION_META = {
  added: { glyph: "+", label: "added" },
  reinforced: { glyph: "↑", label: "reinforced" },
  revised: { glyph: "~", label: "revised" },
};
const CONSOLE_LAYOUT_STORAGE_KEY = "thesis.agentconsole.layout.v1";
const CONSOLE_LAYOUT_DEFAULTS = Object.freeze({
  leftWidth: 220,
  rightWidth: 260,
  ledgerPercent: 34,
  leftCollapsed: false,
  rightCollapsed: false,
  centerMode: "split",
  ledgerOpen: true,
  ledgerSurface: "translation",
  ledgerView: "changes",
  translationLayout: "target",
});
const CONSOLE_LAYOUT_LIMITS = Object.freeze({
  leftMin: 176,
  leftMax: 380,
  rightMin: 220,
  rightMax: 480,
  ledgerMin: 20,
  ledgerMax: 74,
});
const CONSOLE_LOCALE_STORAGE_KEY = "thesis.agentconsole.locale.v1";
const CONSOLE_UI_LOCALES = new Set(["vi", "en"]);
const CONSOLE_UI_TEXT = Object.freeze({
  vi: Object.freeze({
    workspace: "Workspace",
    selectRun: "Chọn run",
    translate: "Dịch",
    replay: "Phát lại",
    play: "Phát",
    pause: "Tạm dừng",
    refresh: "Làm mới",
    pauseAfterStage: "Dừng sau tầng",
    resume: "Tiếp tục",
    cancel: "Hủy",
    theme: "Giao diện",
    resetLayout: "Đặt lại bố cục",
    uiLanguage: "Ngôn ngữ giao diện",
    latestArtifact: "artifact mới nhất",
    results: "kết quả",
    noneYet: "chưa có",
    gates: "gates",
    noScores: "Chưa có điểm — kết quả sẽ hiện sau khi tầng chấm điểm hoàn tất.",
    gateFail: "Gate FAIL",
    gatePass: "Gate PASS",
    seeReport: "xem báo cáo",
    metricHelp: "Chú thích thang điểm",
    definition: "Ý nghĩa",
    direction: "Chiều tốt",
    scope: "Phạm vi",
    unit: "Đơn vị",
    artifactSource: "Nguồn artifact",
    higherBetter: "Càng cao càng tốt",
    lowerBetter: "Càng thấp càng tốt",
    descriptiveOnly: "Chỉ số mô tả",
    unverifiedDefinition: "Chưa có định nghĩa đã xác minh cho metric này trong hợp đồng hiện tại.",
    unverifiedValue: "Chưa xác minh",
    gap: "Chênh lệch",
    importantEvents: "Quan trọng",
    allEvents: "Tất cả",
    eventPreset: "Mức sự kiện",
    mode: "Chế độ",
    state: "Trạng thái",
    phaseReady: "Phase 1 đã sẵn sàng",
    replayCursorAt: "Replay hiện ở",
    liveCursorAt: "Live hiện ở",
    recordedCursorAt: "Bản ghi hiện ở",
    skippedReason: "Lý do bỏ qua",
    rawReason: "Mã gốc",
    resumeDigestMatchExplanation: "Đầu vào khớp checkpoint đã lưu, nên tầng này dùng lại kết quả cũ thay vì chạy lại.",
    unknownSkipReason: "Runtime đã bỏ qua tầng này; chưa có diễn giải thân thiện cho mã lý do này.",
    navigationBlockReady: "Đã mở block tại replay cursor hiện tại",
    navigationMemoryReady: "Đã mở thay đổi bộ nhớ đã commit",
    navigationArtifactReady: "Đã chọn artifact đã xuất hiện",
    navigationStageFilter: "Đang lọc Event Stream theo tầng",
    navigationTargetUnavailable: "Đích chưa khả dụng tại replay cursor hiện tại",
    clearNavigation: "Bỏ chọn",
  }),
  en: Object.freeze({
    workspace: "Workspace",
    selectRun: "Select run",
    translate: "Translate",
    replay: "Replay",
    play: "Play",
    pause: "Pause",
    refresh: "Refresh",
    pauseAfterStage: "Pause after stage",
    resume: "Resume",
    cancel: "Cancel",
    theme: "Theme",
    resetLayout: "Reset layout",
    uiLanguage: "Interface language",
    latestArtifact: "latest artifact",
    results: "results",
    noneYet: "none yet",
    gates: "gates",
    noScores: "No scores yet — results appear after the scoring stage completes.",
    gateFail: "Gate FAIL",
    gatePass: "Gate PASS",
    seeReport: "see report",
    metricHelp: "Metric explanation",
    definition: "Meaning",
    direction: "Preferred direction",
    scope: "Scope",
    unit: "Unit",
    artifactSource: "Artifact source",
    higherBetter: "Higher is better",
    lowerBetter: "Lower is better",
    descriptiveOnly: "Descriptive metric",
    unverifiedDefinition: "No verified definition is registered for this metric in the current contract.",
    unverifiedValue: "Unverified",
    gap: "Gap",
    importantEvents: "Important",
    allEvents: "All",
    eventPreset: "Event level",
    mode: "Mode",
    state: "State",
    phaseReady: "Phase 1 ready",
    replayCursorAt: "Replay cursor at",
    liveCursorAt: "Live cursor at",
    recordedCursorAt: "Recorded cursor at",
    skippedReason: "Skip reason",
    rawReason: "Raw code",
    resumeDigestMatchExplanation: "The input matches the saved checkpoint, so the persisted result is reused instead of rerunning the stage.",
    unknownSkipReason: "The runtime skipped this stage; no friendly explanation is registered for this reason code.",
    navigationBlockReady: "Opened the block at the current replay cursor",
    navigationMemoryReady: "Opened the committed memory change",
    navigationArtifactReady: "Selected an artifact already emitted",
    navigationStageFilter: "Event Stream filtered by stage",
    navigationTargetUnavailable: "Target is not available at the current replay cursor",
    clearNavigation: "Clear selection",
  }),
});
const CONSOLE_METRIC_GLOSSARY = Object.freeze({
  TC: Object.freeze({
    shortCode: "TC",
    canonicalName: "Term Consistency",
    explanation: Object.freeze({
      vi: "Đo mức một thuật ngữ được dịch nhất quán xuyên suốt các lần xuất hiện; không đánh giá bản dịch đó có đúng chuẩn từ điển hay không.",
      en: "Measures whether the same term is rendered consistently across its occurrences; it does not judge whether that rendering matches an external standard.",
    }),
    direction: "higher",
    scope: "block-level aggregate",
    unit: "ratio [0, 1]",
    artifactSource: "score report",
  }),
  TA: Object.freeze({
    shortCode: "TA",
    canonicalName: "Term Adherence",
    explanation: Object.freeze({
      vi: "Đo mức bản dịch thuật ngữ khớp với các dạng được chấp nhận trong chuẩn đối chiếu của run.",
      en: "Measures how often term renderings match accepted forms in the run's reference standard.",
    }),
    direction: "higher",
    scope: "occurrence-weighted aggregate",
    unit: "ratio [0, 1]",
    artifactSource: "score report",
  }),
  TA_REGISTRY: Object.freeze({
    shortCode: "TA-Registry",
    canonicalName: "Registry Adherence",
    explanation: Object.freeze({
      vi: "Đo mức bản dịch tuân thủ các chỉ dẫn thuật ngữ đã được đưa vào registry/context của chính run.",
      en: "Measures how closely the translation follows terminology directives persisted in the run registry or context.",
    }),
    direction: "higher",
    scope: "run registry",
    unit: "ratio [0, 1]",
    artifactSource: "score report",
  }),
  TC_OCC: Object.freeze({
    shortCode: "TC-Occ",
    canonicalName: "Term Consistency at Occurrence Level",
    explanation: Object.freeze({
      vi: "Tính theo từng lần xuất hiện đã định vị: tỷ lệ occurrence dùng cách dịch chiếm đa số của chính thuật ngữ đó.",
      en: "Occurrence-level consistency: the share of localized occurrences using that term's majority rendering.",
    }),
    direction: "higher",
    scope: "localized occurrences",
    unit: "ratio [0, 1]",
    artifactSource: "occurrence localization report",
  }),
  TA_OCC: Object.freeze({
    shortCode: "TA-Occ",
    canonicalName: "Term Adherence at Occurrence Level",
    explanation: Object.freeze({
      vi: "Tính theo từng lần xuất hiện đã định vị: tỷ lệ occurrence rơi vào một dạng dịch được chấp nhận.",
      en: "Occurrence-level adherence: the share of localized occurrences landing on an accepted rendering.",
    }),
    direction: "higher",
    scope: "localized occurrences",
    unit: "ratio [0, 1]",
    artifactSource: "occurrence localization report",
  }),
  SF_QE: Object.freeze({
    shortCode: "SF-QE",
    canonicalName: "Semantic Fidelity — Quality Estimation",
    explanation: Object.freeze({
      vi: "Ước lượng độ trung thành ngữ nghĩa bằng mô hình Quality Estimation không cần bản dịch tham chiếu; đây là bằng chứng hội tụ, không phải phán quyết duy nhất.",
      en: "Reference-free quality-estimation evidence for semantic fidelity; it is convergent evidence, not a sole judge.",
    }),
    direction: "higher",
    scope: "translated segments",
    unit: "model score",
    artifactSource: "SF-QE report",
  }),
  SF_BT: Object.freeze({
    shortCode: "SF-BT",
    canonicalName: "Semantic Fidelity — Back Translation",
    explanation: Object.freeze({
      vi: "Đánh giá độ trung thành qua bản dịch ngược. Hệ thống giữ các thành phần con riêng, không tự gộp thành một điểm tổng.",
      en: "Back-translation evidence for semantic fidelity. Component scores remain separate rather than being collapsed into one composite.",
    }),
    direction: "higher",
    scope: "translated segments",
    unit: "component score",
    artifactSource: "SF-BT report",
  }),
  SF_BT_COS: Object.freeze({
    shortCode: "SF-BT-cos",
    canonicalName: "Back-Translation Cosine Similarity",
    explanation: Object.freeze({
      vi: "Độ tương đồng cosine giữa nguồn và nội dung dịch ngược trong không gian biểu diễn.",
      en: "Cosine similarity between the source and back-translated content in representation space.",
    }),
    direction: "higher",
    scope: "translated segments",
    unit: "similarity [0, 1]",
    artifactSource: "SF-BT report",
  }),
  SF_BT_LLM: Object.freeze({
    shortCode: "SF-BT-llm",
    canonicalName: "Back-Translation LLM Judgment",
    explanation: Object.freeze({
      vi: "Phán đoán của LLM về mức bảo toàn ý nghĩa giữa nguồn và bản dịch ngược.",
      en: "LLM judgment of semantic preservation between the source and back translation.",
    }),
    direction: "higher",
    scope: "translated segments",
    unit: "model score",
    artifactSource: "SF-BT report",
  }),
});

function consoleClamp(value, min, max, fallback = min) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.min(max, Math.max(min, numeric)) : fallback;
}

function consoleReadLayout() {
  if (typeof window === "undefined" || !window.localStorage) return { ...CONSOLE_LAYOUT_DEFAULTS };
  try {
    const raw = JSON.parse(window.localStorage.getItem(CONSOLE_LAYOUT_STORAGE_KEY) || "null");
    if (!raw || typeof raw !== "object") return { ...CONSOLE_LAYOUT_DEFAULTS };
    const centerMode = ["split", "events", "ledger"].includes(raw.centerMode)
      ? raw.centerMode
      : CONSOLE_LAYOUT_DEFAULTS.centerMode;
    const ledgerSurface = ["memory", "translation"].includes(raw.ledgerSurface)
      ? raw.ledgerSurface
      : CONSOLE_LAYOUT_DEFAULTS.ledgerSurface;
    const ledgerView = ["changes", "current", "pending"].includes(raw.ledgerView)
      ? raw.ledgerView
      : CONSOLE_LAYOUT_DEFAULTS.ledgerView;
    const translationLayout = ["target", "parallel", "triple"].includes(raw.translationLayout)
      ? raw.translationLayout
      : CONSOLE_LAYOUT_DEFAULTS.translationLayout;
    return {
      leftWidth: consoleClamp(raw.leftWidth, CONSOLE_LAYOUT_LIMITS.leftMin, CONSOLE_LAYOUT_LIMITS.leftMax, CONSOLE_LAYOUT_DEFAULTS.leftWidth),
      rightWidth: consoleClamp(raw.rightWidth, CONSOLE_LAYOUT_LIMITS.rightMin, CONSOLE_LAYOUT_LIMITS.rightMax, CONSOLE_LAYOUT_DEFAULTS.rightWidth),
      ledgerPercent: consoleClamp(raw.ledgerPercent, CONSOLE_LAYOUT_LIMITS.ledgerMin, CONSOLE_LAYOUT_LIMITS.ledgerMax, CONSOLE_LAYOUT_DEFAULTS.ledgerPercent),
      leftCollapsed: raw.leftCollapsed === true,
      rightCollapsed: raw.rightCollapsed === true,
      centerMode,
      ledgerOpen: raw.ledgerOpen !== false,
      ledgerSurface,
      ledgerView,
      translationLayout,
    };
  } catch (_) {
    return { ...CONSOLE_LAYOUT_DEFAULTS };
  }
}

function consoleWriteLayout(layout) {
  if (typeof window === "undefined" || !window.localStorage) return;
  try {
    window.localStorage.setItem(CONSOLE_LAYOUT_STORAGE_KEY, JSON.stringify(layout));
  } catch (_) {
    // Layout persistence is optional; storage restrictions must not break Console.
  }
}

function consoleReadLocale() {
  if (typeof window !== "undefined" && window.ThesisI18n) return window.ThesisI18n.getLocale();
  if (typeof window === "undefined" || !window.localStorage) return "vi";
  try {
    const stored = String(window.localStorage.getItem(CONSOLE_LOCALE_STORAGE_KEY) || "").toLowerCase();
    return CONSOLE_UI_LOCALES.has(stored) ? stored : "vi";
  } catch (_) {
    return "vi";
  }
}

function consoleWriteLocale(locale) {
  if (typeof window !== "undefined" && window.ThesisI18n) {
    window.ThesisI18n.setLocale(locale);
    return;
  }
  if (typeof window === "undefined" || !window.localStorage || !CONSOLE_UI_LOCALES.has(locale)) return;
  try {
    window.localStorage.setItem(CONSOLE_LOCALE_STORAGE_KEY, locale);
  } catch (_) {
    // Locale persistence is optional; storage restrictions must not break Console.
  }
}

function consoleText(locale, key) {
  const table = CONSOLE_UI_TEXT[CONSOLE_UI_LOCALES.has(locale) ? locale : "vi"];
  return table[key] || CONSOLE_UI_TEXT.en[key] || key;
}

const CONSOLE_IMPORTANT_EVENTS = new Set([
  "run_start", "run_resumed", "run_done", "run_failed", "run_cancelled",
  "stage_start", "stage_done", "stage_failed", "stage_skipped",
  "component_started", "component_resumed", "component_halted", "component_done", "component_failed",
  "stage_started", "stage_progress", "stage_completed",
  "checkpoint", "run_committed", "artifact_created", "health_check",
  "window_preview_available", "block_done", "memory_delta", "retry",
  "warning", "error",
]);

function consoleIsImportantEvent(row) {
  const event = String(row?.event || "");
  return row?.severity === "warning"
    || row?.severity === "error"
    || CONSOLE_IMPORTANT_EVENTS.has(event)
    || event.startsWith("gate_")
    || event.includes("commit");
}

function consoleCurrentStageId(rows, stagePlan = CONSOLE_STAGE_PLAN) {
  const stageIds = new Set(stagePlan.map(stage => stage.id));
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const stage = String(rows[index]?.stage || "");
    if (stageIds.has(stage)) return stage;
  }
  return "";
}

function consoleSkipReasonExplanation(reason, locale) {
  return String(reason || "") === "resume_digest_match"
    ? consoleText(locale, "resumeDigestMatchExplanation")
    : consoleText(locale, "unknownSkipReason");
}

function consoleMetricDescriptor(metricKey) {
  const rawKey = String(metricKey || "").trim();
  const armMatch = rawKey.match(/_(S0|S1)$/i);
  const arm = armMatch ? armMatch[1].toUpperCase() : "";
  const baseKey = armMatch ? rawKey.slice(0, -armMatch[0].length) : rawKey;
  const normalizedKey = baseKey.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return {
    rawKey,
    arm,
    normalizedKey,
    meta: CONSOLE_METRIC_GLOSSARY[normalizedKey] || null,
  };
}

function ConsoleMetricLabel({ metricKey, locale, fallbackLabel = "", prefix = "", suffix = "", idSuffix = "" }) {
  const descriptor = consoleMetricDescriptor(metricKey);
  const meta = descriptor.meta;
  const code = meta ? meta.shortCode : (descriptor.rawKey || fallbackLabel || "?");
  const canonicalName = meta ? meta.canonicalName : "";
  const visibleFallback = !meta && fallbackLabel ? fallbackLabel : "";
  const tooltipId = `console-metric-${String(metricKey || "unknown").replace(/[^a-z0-9]+/gi, "-")}-${String(idSuffix || "value").replace(/[^a-z0-9]+/gi, "-")}`;
  const direction = meta
    ? consoleText(locale, meta.direction === "higher" ? "higherBetter" : meta.direction === "lower" ? "lowerBetter" : "descriptiveOnly")
    : consoleText(locale, "unverifiedValue");

  return (
    <button
      className="metric-help"
      type="button"
      aria-describedby={tooltipId}
      aria-label={`${consoleText(locale, "metricHelp")}: ${canonicalName || visibleFallback || code}`}
    >
      {prefix && <span className="metric-prefix">{prefix}</span>}
      {descriptor.arm && <span className="metric-arm">{descriptor.arm}</span>}
      <span className="metric-code">{visibleFallback || code}</span>
      {canonicalName && <span className="metric-canonical">· {canonicalName}</span>}
      {suffix && <span className="metric-suffix">{suffix}</span>}
      <span className="metric-popover" id={tooltipId} role="tooltip">
        <span className="metric-popover-title">
          <span className="metric-popover-code">{code}</span>
          <span>{canonicalName || visibleFallback || descriptor.rawKey}</span>
        </span>
        <span className="metric-popover-definition">
          {meta ? meta.explanation[locale] || meta.explanation.en : consoleText(locale, "unverifiedDefinition")}
        </span>
        <span className="metric-popover-grid">
          <span>{consoleText(locale, "direction")}</span><strong>{direction}</strong>
          <span>{consoleText(locale, "scope")}</span><strong>{meta ? meta.scope : consoleText(locale, "unverifiedValue")}</strong>
          <span>{consoleText(locale, "unit")}</span><strong>{meta ? meta.unit : consoleText(locale, "unverifiedValue")}</strong>
          <span>{consoleText(locale, "artifactSource")}</span><strong>{meta ? meta.artifactSource : consoleText(locale, "unverifiedValue")}</strong>
        </span>
      </span>
    </button>
  );
}

function ConsoleSkipReason({ reason, locale, stageLabel }) {
  const rawReason = String(reason || "");
  const tooltipId = `console-skip-${String(stageLabel || "stage").replace(/[^a-z0-9]+/gi, "-")}-${rawReason.replace(/[^a-z0-9]+/gi, "-") || "unknown"}`;
  return (
    <button
      className="stage-skip-help"
      type="button"
      aria-describedby={tooltipId}
      aria-label={`${consoleText(locale, "skippedReason")}: ${rawReason}`}
    >
      <span>{uiText("đã bỏ qua", "skipped")}</span>
      <span className="stage-skip-mark" aria-hidden="true">?</span>
      <span className="stage-skip-popover" id={tooltipId} role="tooltip">
        <strong>{stageLabel}</strong>
        <span>{consoleSkipReasonExplanation(rawReason, locale)}</span>
        <span className="stage-skip-code">
          <span>{consoleText(locale, "rawReason")}</span>
          <code>{rawReason}</code>
        </span>
      </span>
    </button>
  );
}

function consoleIsTerminalStatus(status) {
  return CONSOLE_TERMINAL_STATUSES.has(String(status || "").toLowerCase());
}

function consoleEventSeverity(row) {
  const sev = String(row.severity || "").toLowerCase();
  if (sev === "error" || sev === "warning") return sev;
  // context-ish events get the amber ⊙ treatment (e.g. cost snapshots, gate pause)
  if (row.event === "gate_pause") return "warning";
  return "info";
}

function consoleShort(value, n = 46) {
  const s = String(value == null ? "" : value);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function consoleMemoryProjection(value) {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return consoleShort(value, 120);
  }
  if (typeof value !== "object" || Array.isArray(value)) return "";
  const preferred = ["display", "canonical", "target", "aliases", "value", "label", "status"];
  const compact = preferred
    .filter(key => value[key] != null && ["string", "number", "boolean"].includes(typeof value[key]))
    .map(key => `${key}: ${value[key]}`);
  if (compact.length) return consoleShort(compact.join(" · "), 120);
  const scalar = Object.entries(value)
    .filter(([, item]) => item == null || ["string", "number", "boolean"].includes(typeof item))
    .slice(0, 4)
    .map(([key, item]) => `${key}: ${item == null ? "null" : item}`);
  return consoleShort(scalar.join(" · "), 120);
}

/* memory_delta_v1 is a small, read-only UI event. The console deliberately
   refuses unknown enum values and never infers deltas by diffing registries or
   interpreting raw model output. */
function consoleMemoryDelta(raw, payload) {
  if (!payload || payload.contract !== "memory_delta_v1") return null;
  const domain = String(payload.domain || "");
  const collection = String(payload.collection || "");
  const operation = String(payload.operation || "");
  const lifecycle = String(payload.lifecycle || "");
  const recordId = String(payload.record_id || "").trim();
  const label = String(payload.label || "").trim();
  const deltaId = String(payload.delta_id || "").trim();
  const revisionBefore = payload.revision_before == null ? null : Number(payload.revision_before);
  const revisionAfter = Number(payload.revision_after);
  const recordHashBefore = payload.record_hash_before == null
    ? null
    : String(payload.record_hash_before || "").trim();
  const recordHashAfter = String(payload.record_hash_after || "").trim();
  const receipt = payload.commit_receipt && typeof payload.commit_receipt === "object"
    ? payload.commit_receipt
    : null;
  const receiptId = String((receipt && receipt.receipt_id) || "").trim();
  const stateGeneration = Number(receipt && receipt.state_generation);
  const domainCollections = CONSOLE_MEMORY_DOMAIN_COLLECTIONS[domain];
  const sha256Pattern = /^[0-9a-f]{64}$/i;
  if (!["terminology", "literary"].includes(domain)
      || !domainCollections
      || !domainCollections.has(collection)
      || !CONSOLE_MEMORY_COLLECTIONS.has(collection)
      || !CONSOLE_MEMORY_OPERATIONS.has(operation)
      || !CONSOLE_MEMORY_LIFECYCLES.has(lifecycle)
      || !recordId
      || !label
      || !deltaId
      || !Number.isInteger(revisionAfter)
      || revisionAfter < 1
      || !sha256Pattern.test(recordHashAfter)
      || !receiptId
      || !Number.isInteger(stateGeneration)
      || stateGeneration < 1) return null;

  if (operation === "added") {
    if (revisionBefore != null || recordHashBefore != null) return null;
  } else if (!Number.isInteger(revisionBefore)
      || revisionBefore < 1
      || revisionBefore >= revisionAfter
      || !sha256Pattern.test(recordHashBefore || "")) {
    return null;
  }

  const refs = Array.isArray(payload.source_refs)
    ? payload.source_refs.slice(0, 4).map(ref => ({
      chapterId: String((ref && ref.chapter_id) || ""),
      blockId: String((ref && ref.block_id) || ""),
    })).filter(ref => ref.chapterId && ref.blockId)
    : [];
  if (!refs.length) return null;
  const evidenceDelta = Number(payload.evidence_delta);
  return {
    key: deltaId,
    ts: String(raw.ts || ""),
    stage: String(raw.stage || ""),
    agent: String(raw.agent || ""),
    domain,
    collection,
    operation,
    lifecycle,
    deltaId,
    recordId,
    revisionBefore,
    revisionAfter,
    receiptId,
    stateGeneration,
    label: consoleShort(label, 80),
    before: consoleMemoryProjection(payload.before),
    after: consoleMemoryProjection(payload.after),
    evidenceDelta: Number.isFinite(evidenceDelta) ? evidenceDelta : null,
    reasonCode: consoleShort(payload.reason_code || "", 60),
    sourceRefs: refs,
  };
}

function consoleMemoryDeltaMessage(delta) {
  if (!delta) return "invalid memory delta";
  const meta = CONSOLE_MEMORY_OPERATION_META[delta.operation] || { label: delta.operation };
  return `${delta.collection} · ${meta.label} · ${delta.label}`;
}

function consoleBaseName(p) {
  if (!p) return "";
  const s = String(p).replace(/\\/g, "/");
  return s.slice(s.lastIndexOf("/") + 1);
}

function consoleDuration(start, end) {
  if (!start || !end) return "";
  const a = Date.parse(start);
  const b = Date.parse(end);
  if (isNaN(a) || isNaN(b) || b < a) return "";
  const seconds = Math.round((b - a) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${String(seconds % 60).padStart(2, "0")}`;
}

function consoleSystemLabel(id) {
  const key = String(id || "").trim();
  if (CONSOLE_SYSTEM_LABELS[key]) return CONSOLE_SYSTEM_LABELS[key];
  return key ? key.replace(/_/g, " ") : uiText("Kiểm tra chưa rõ", "Unknown check");
}

function consoleSystemDetail(check) {
  if (!check) return uiText("Chưa kiểm tra", "Not checked");
  if (check.id === "openai_key" || check.id === "gemini_key") {
    return check.ok === false ? uiText("Thiếu credential", "Credential missing") : uiText("Đã cấu hình credential · chưa gọi provider", "Credential configured · provider not called");
  }
  const matched = Array.isArray(check.matchedModels) && check.matchedModels.length ? check.matchedModels[0] : "";
  const model = matched || check.expectedModel;
  if (model && check.endpoint) return `${model} · ${check.endpoint}`;
  if (model) return model;
  if (check.module) return `${check.module}${check.python ? " · Python " + check.python : ""}`;
  if (check.endpoint) return check.endpoint;
  return check.ok === false ? uiText("Preflight thất bại", "Preflight failed") : check.ok === true ? uiText("Preflight đạt", "Preflight passed") : uiText("Kết quả không khả dụng", "Result unavailable");
}

function consoleSystemStateLabel(check) {
  if (!check || check.ok == null) return uiText("CHƯA RÕ", "UNKNOWN");
  if (check.id === "openai_key" || check.id === "gemini_key") return check.ok ? uiText("KEY SẴN SÀNG", "KEY READY") : uiText("BỊ THIẾU", "MISSING");
  if (check.id === "cometkiwi_import") return check.ok ? uiText("ĐÃ TẢI", "LOADED") : uiText("THẤT BẠI", "FAILED");
  return check.ok ? uiText("SẴN SÀNG", "READY") : uiText("THẤT BẠI", "FAILED");
}

function consoleSystemTime(ts) {
  const parsed = Date.parse(String(ts || ""));
  if (!Number.isFinite(parsed)) return "";
  const date = new Date(parsed);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}`;
}

function consoleSystemChecks(checks) {
  return Object.values(checks || {}).sort((a, b) => {
    const ai = CONSOLE_SYSTEM_ORDER.indexOf(a.id);
    const bi = CONSOLE_SYSTEM_ORDER.indexOf(b.id);
    if (ai !== -1 || bi !== -1) return (ai === -1 ? CONSOLE_SYSTEM_ORDER.length : ai) - (bi === -1 ? CONSOLE_SYSTEM_ORDER.length : bi);
    return consoleSystemLabel(a.id).localeCompare(consoleSystemLabel(b.id));
  });
}

function consoleReplayTimestampMs(event) {
  const parsed = Date.parse(event && event.ts ? event.ts : "");
  return Number.isFinite(parsed) ? parsed : null;
}

function consoleReplayDelay(events, cursor, mode, speed) {
  const factor = Number(speed) > 0 ? Number(speed) : 1;
  if (mode !== "time" || cursor <= 0) return CONSOLE_REPLAY_EVENT_STEP_MS / factor;
  const previous = consoleReplayTimestampMs(events[cursor - 1]);
  const next = consoleReplayTimestampMs(events[cursor]);
  if (previous == null || next == null || next < previous) return CONSOLE_REPLAY_EVENT_STEP_MS / factor;
  const bounded = Math.min(CONSOLE_REPLAY_MAX_GAP_MS, Math.max(CONSOLE_REPLAY_MIN_GAP_MS, next - previous));
  return bounded / factor;
}

function consoleReplayClock(events, cursor) {
  if (!events.length) return { elapsed: "0:00", total: "0:00" };
  const first = consoleReplayTimestampMs(events[0]);
  const last = consoleReplayTimestampMs(events[events.length - 1]);
  const currentIndex = Math.max(0, Math.min(events.length - 1, Number(cursor || 0) - 1));
  const current = consoleReplayTimestampMs(events[currentIndex]);
  const format = ms => {
    const seconds = Math.max(0, Math.round(Number(ms || 0) / 1000));
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
  };
  if (first == null || last == null || current == null || last < first) {
    return {
      elapsed: format(Number(cursor || 0) * CONSOLE_REPLAY_EVENT_STEP_MS),
      total: format(events.length * CONSOLE_REPLAY_EVENT_STEP_MS),
    };
  }
  return { elapsed: format(current - first), total: format(last - first) };
}

function consoleHeartbeatGroupKey(row) {
  if (!row || row.event !== "heartbeat") return "";
  return [
    row.attempt == null ? "" : row.attempt,
    row.stage || "",
    row.agent || "",
    row.heartbeatPid == null ? "" : row.heartbeatPid,
    row.heartbeatBaseMessage || row.message || "",
  ].join("|");
}

/* Display-only compaction. Raw events remain untouched for replay, health state,
   event counts, export, and audit. Only consecutive equivalent heartbeats fold. */
function consoleHeartbeatRows(rows, mode = "grouped") {
  const resolvedMode = CONSOLE_HEARTBEAT_MODES.has(mode) ? mode : "grouped";
  if (resolvedMode === "raw") return rows;
  if (resolvedMode === "hidden") return rows.filter(row => row.event !== "heartbeat");

  const result = [];
  rows.forEach(row => {
    if (row.event !== "heartbeat") {
      result.push(row);
      return;
    }

    const groupKey = consoleHeartbeatGroupKey(row);
    const previous = result[result.length - 1];
    if (!previous || previous.event !== "heartbeat" || previous.heartbeatGroupKey !== groupKey) {
      result.push({
        ...row,
        heartbeatGroupKey: groupKey,
        heartbeatCount: 1,
        heartbeatFirstTs: row.ts,
        heartbeatLastTs: row.ts,
        heartbeatFirstLineNo: row.lineNo,
        heartbeatLastLineNo: row.lineNo,
        heartbeatFirstSeq: row.seq,
        heartbeatLastSeq: row.seq,
        rawEventCount: 1,
      });
      return;
    }

    const count = Number(previous.heartbeatCount || 1) + 1;
    const duration = consoleDuration(previous.heartbeatFirstTs, row.ts);
    result[result.length - 1] = {
      ...previous,
      key: previous.key,
      ts: row.ts,
      seq: row.seq,
      lineNo: row.lineNo,
      heartbeatCount: count,
      heartbeatLastTs: row.ts,
      heartbeatLastLineNo: row.lineNo,
      heartbeatLastSeq: row.seq,
      rawEventCount: count,
      message: `${previous.heartbeatBaseMessage || previous.message}${duration ? ` · ${duration} elapsed` : ""}`,
    };
  });
  return result;
}

function consoleEventSequenceLabel(row) {
  const firstLine = row.heartbeatFirstLineNo;
  const lastLine = row.heartbeatLastLineNo;
  const line = firstLine != null && lastLine != null && firstLine !== lastLine
    ? `#${firstLine}-${lastLine}`
    : `#${row.lineNo != null ? row.lineNo : "-"}`;
  if ((row.attempt == null && row.attemptIndex == null) || row.seq == null) return line;
  const firstSeq = row.heartbeatFirstSeq;
  const lastSeq = row.heartbeatLastSeq;
  const seq = firstSeq != null && lastSeq != null && firstSeq !== lastSeq
    ? `${firstSeq}-${lastSeq}`
    : row.seq;
  const attemptIndex = row.attemptIndex ?? row.attempt;
  const attemptIdentity = row.attempt != null && String(row.attempt) !== String(attemptIndex)
    ? `${attemptIndex}:${row.attempt}`
    : attemptIndex;
  return `${line} · a${attemptIdentity}/${seq}`;
}

function formatConsoleMetric(value, unit) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (unit === "ratio") return n.toFixed(3);
  return Math.abs(n) >= 10 ? n.toFixed(2) : n.toFixed(4);
}

function formatConsoleSignedRatio(value) {
  if (value == null || value === "") return "?";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return (n >= 0 ? "+" : "") + n.toFixed(3);
}

function formatConsoleGate(gate) {
  if (!gate || !gate.present) return null;
  const passed = Number(gate.passed || 0);
  const total = Number(gate.total || 0);
  const failed = Array.isArray(gate.failed) ? gate.failed : [];
  if (gate.all_ok === false) return `${passed}/${total} fail ${consoleShort(failed.join(", ") || "invariant", 28)}`;
  return `${passed}/${total} ok`;
}

function consolePackMessage(summary, ctx) {
  if (!summary || typeof summary !== "object") return null;
  const injected = summary.injected ?? summary.included_count;
  if (injected == null) return null;
  const dropped = summary.dropped_by_budget ?? summary.dropped_by_budget_count ?? 0;
  const excluded = summary.excluded ?? summary.excluded_count ?? null;
  const tokens = summary.est_tokens ?? summary.token_estimate ?? null;
  const parts = [`window ${ctx.win || "?"} · pack ${injected} inj`];
  const detail = [];
  if (summary.mandatory != null) detail.push(`${summary.mandatory} mand`);
  if (summary.soft != null) detail.push(`${summary.soft} soft`);
  if (summary.preserve != null) detail.push(`${summary.preserve} preserve`);
  if (summary.quarantine) detail.push(`${summary.quarantine} quarantine`);
  if (summary.address) detail.push(`${summary.address} address`);
  if (excluded) detail.push(`${excluded} excl`);
  if (detail.length) parts.push(`(${detail.join("/")})`);
  parts.push(`· ${dropped} drop`);
  if (tokens != null) parts.push(`· ${tokens}tok`);
  return parts.join(" ");
}

function consoleConsistencyTierRows(consistency) {
  if (!consistency || !consistency.present) return [];
  const configs = consistency.configs || [];
  const byTier = consistency.by_tier || {};
  const notable = consistency.notable_terms || [];
  return CONSOLE_INJECTED_TIERS.map(tier => {
    const s0 = byTier.S0 && byTier.S0[tier];
    const s1 = byTier.S1 && byTier.S1[tier];
    const fallbackCfg = configs.find(cfg => byTier[cfg] && byTier[cfg][tier] && Number(byTier[cfg][tier].terms || 0) > 0);
    const fallback = fallbackCfg ? byTier[fallbackCfg][tier] : null;
    const fixedCount = notable.filter(item => item.fixed_by_injection && item.tier === tier).length;
    if (!s0 && !s1 && !fallback) return null;
    return { tier, s0, s1, fallbackCfg, fallback, fixedCount };
  }).filter(Boolean);
}

function consoleWatchReasonLabel(item) {
  const label = String(item.audit_label || item.label || "").trim();
  const reasons = Array.isArray(item.watchlist_reasons) ? item.watchlist_reasons.map(String) : [];
  const joined = [label, ...reasons].join(" ").toLowerCase();
  if (item.collision_soft_fallback || joined.includes("collision")) return "collision";
  if (joined.includes("polysemy") || joined.includes("audit_polysemy") || joined.includes("context")) return "polysemy · ngữ cảnh";
  return label || reasons[0] || "held";
}

function consoleWatchInjectionLabel(item) {
  const action = String(item.injection_action || item.action || "").trim();
  if (!action) return "held";
  if (action === "context_sensitive_translate") return "soft · do-not-force";
  if (action === "translate" || action === "hard_translate") return "mandatory";
  if (action === "preserve") return "preserve";
  if (action === "deprioritize") return "report-only";
  if (action === "review_only") return "review-only";
  return action;
}

function consoleWatchBlockRef(blockId) {
  const text = String(blockId || "").trim();
  const match = text.match(/_b(\d+)$/);
  return match ? `b${match[1]}` : text;
}

function consoleWatchCandidateLabel(candidate) {
  const text = String(candidate && candidate.text || "").trim();
  const block = consoleWatchBlockRef(candidate && candidate.evidence_block_id);
  return block ? `${text} (${block})` : text;
}

function consoleWatchCandidatesLine(item) {
  const candidates = Array.isArray(item.candidates) ? item.candidates : [];
  const competitors = Array.isArray(item.competitors) && item.competitors.length
    ? item.competitors
    : candidates.filter(candidate => String(candidate && candidate.source || "") !== "canonical");
  const canonicalCandidate = candidates.find(candidate => String(candidate && candidate.source || "") === "canonical");
  const canonical = String((canonicalCandidate && canonicalCandidate.text) || item.canonical_target_vi || item.vi || item.target || item.canonical || "").trim();
  const shown = competitors.slice(0, 3).map(consoleWatchCandidateLabel).filter(Boolean);
  const more = competitors.length > shown.length ? ` +${competitors.length - shown.length}` : "";
  if (!canonical && !shown.length) return "";
  if (!shown.length) return `canonical: ${canonical}`;
  return `canonical: ${canonical || "?"} · vs: ${shown.join(", ")}${more}`;
}

function consoleWatchEvidenceLine(item) {
  const evidenceBlocks = Number(item.evidence_blocks || 0) || (Array.isArray(item.evidence_block_ids) ? item.evidence_block_ids.length : 0);
  const btCalls = Number(item.backtranslation_calls || 0);
  const pieces = [];
  if (evidenceBlocks) pieces.push(`${evidenceBlocks} blocks`);
  if (btCalls) pieces.push(`${btCalls} BT`);
  return pieces.join(" / ");
}

/* One-line human message per real event type; falls back to a generic label. */
function consoleMessageFor(row, ctx) {
  const p = row.payload || {};
  switch (row.event) {
    case "run_start": return `run bắt đầu · ${(p.chapters || []).join(", ") || p.job_id || ""}`;
    case "run_resumed": return `resume run · attempt ${p.attempt_id || "?"}`;
    case "run_done": return `run kết thúc: ${p.status || "done"}`;
    case "run_failed": return `run FAILED · ${consoleShort(p.error, 60)}`;
    case "run_cancelled": return "run huỷ bởi người dùng";
    case "run_committed": return "ghi kết quả dịch vào workdb";
    case "component_started": return `${row.component?.component_id || "component"} bắt đầu`;
    case "component_resumed": return `${row.component?.component_id || "component"} tiếp tục`;
    case "component_halted": return `${row.component?.component_id || "component"} tạm dừng · ${p.reason || p.checkpoint || "checkpoint"}`;
    case "component_done": return `${row.component?.component_id || "component"} hoàn tất`;
    case "component_failed": return `${row.component?.component_id || "component"} thất bại · ${consoleShort(p.error || p.reason, 60)}`;
    case "stage_start": return `${ctx.label || row.stage} bắt đầu`;
    case "stage_started": return `${ctx.label || row.stage} bắt đầu`;
    case "stage_progress": return `${ctx.label || row.stage} · ${p.progress?.completed ?? "?"}/${p.progress?.total ?? "?"} ${p.progress?.unit || ""}`;
    case "stage_done": return `${ctx.label || row.stage} xong · exit ${p.exit_code}`;
    case "stage_completed": return `${ctx.label || row.stage} hoàn tất`;
    case "stage_failed": return `${ctx.label || row.stage} thất bại · ${consoleShort(p.error || p.reason, 60)}`;
    case "stage_skipped": return `${ctx.label || row.stage} bỏ qua · ${p.reason || "resume"}`;
    case "cost_snapshot": {
      if (p.estimated_cumulative_usd != null) return `luỹ kế ~$${Number(p.estimated_cumulative_usd).toFixed(4)} / cap $${Number(p.budget_cap_usd || 0).toFixed(2)}`;
      if (p.estimated_cost_cap_usd != null) return `${ctx.label || row.stage} ước tính cap $${Number(p.estimated_cost_cap_usd).toFixed(4)}`;
      return "cost snapshot";
    }
    case "checkpoint": return `checkpoint · ${p.checkpoint || "saved"}`;
    case "gate_pause": return `tạm dừng · ${p.reason || "gate"}${p.before_stage ? " trước " + p.before_stage : ""}`;
    case "heartbeat": return `alive · ${row.stage}${p.active_child_pid ? " · pid " + p.active_child_pid : ""}`;
    case "health_check": return `${p.id || "check"} · ${p.ok === false ? "FAIL" : "ok"}`;
    case "window_started": return `window ${ctx.win || "?"} bắt đầu`;
    case "prompt_built": return consolePackMessage(p.pack_summary || p.context_summary, ctx) || `window ${ctx.win || "?"} · prompt dựng xong`;
    case "pack_built": return consolePackMessage(p.pack_summary || p.context_summary || p, ctx) || `window ${ctx.win || "?"} · pack dựng xong`;
    case "request_sent": return `window ${ctx.win || "?"} · gọi LLM`;
    case "response_received": return `window ${ctx.win || "?"} · nhận kết quả`;
    case "json_parsed": return `window ${ctx.win || "?"} · parse JSON ok`;
    case "window_preview_available": return `window ${ctx.win || "?"} · bản dịch sẵn sàng`;
    case "persist_buffered": return `window ${ctx.win || "?"} · buffer ghi`;
    case "block_done": return `dịch xong ${p.block_id || ""}`;
    case "llm_call": return `LLM ${p.model || ""}${p.cache_hit ? " · cache hit" : ""}`;
    case "artifact_created": return consoleBaseName(p.artifact_ref || p.artifact_path) || "artifact";
    case "memory_delta": return consoleMemoryDeltaMessage(consoleMemoryDelta(row, p));
    case "warning": return consoleShort(p.message || p.reason, 60);
    case "error": return consoleShort(p.error || p.message, 60);
    case "retry": return `retry ${p.attempt || ""} · ${p.reason || ""}`;
    default: return consoleShort(p.message || p.reason || row.event, 60);
  }
}

/* Pure: turn a raw merged event array into everything the console renders. */
function consoleDeclaredStageStatus(status) {
  if (status === "succeeded") return "done";
  if (status === "running" || status === "paused") return "active";
  if (status === "failed") return "failed";
  return "pending";
}

function deriveConsoleState(events, stagePlan = CONSOLE_STAGE_PLAN, useDeclaredStageStatus = false) {
  const stageInfo = {};
  stagePlan.forEach(s => {
    stageInfo[s.id] = {
      status: useDeclaredStageStatus ? consoleDeclaredStageStatus(s.status) : "pending",
      start: null,
      end: null,
      exit: null,
      previews: 0,
      declaredProgress: s.progress ?? null,
    };
  });
  let winCounter = 0;
  const translatorWindowCounters = {};
  const previewWindowsByArm = {};
  const translatorTotalsByArm = {};
  let cumulativeCost = null;
  let budgetCap = null;
  let warnings = 0, errors = 0;
  let phase1Done = false;
  let runStatus = "";
  let latestArtifact = null;
  let latestPreviewWin = null;
  let latestPreviewArm = "";
  let translatorTotal = null;
  let stderrTail = [];
  let paused = false, pausedReason = "";
  let latestPackSummary = null;
  let latestPackWindow = null;
  const memoryDeltas = [];
  const memoryDeltaIds = new Set();
  let invalidMemoryDeltaCount = 0;
  const systemChecks = {};
  const normalized = [];

  events.forEach((raw, idx) => {
    const payload = raw && typeof raw.payload === "object" && raw.payload ? raw.payload : {};
    const stage = raw.stage || raw.stage_id || "";
    const event = raw.event || raw.event_type || "";
    let memoryDelta = null;
    const eventArm = String(payload.config || payload.arm || payload.variant || "translation");
    const payloadWindowOrdinal = consolePreviewWindowOrdinal(payload.window_id);
    // Keep the global count for old events, but label lifecycle events by their
    // own arm/window. S0 window 7 and S1 window 1 are different cursors.
    if (event === "window_started") {
      winCounter += 1;
      const nextArmWindow = Number(translatorWindowCounters[eventArm] || 0) + 1;
      translatorWindowCounters[eventArm] = Math.max(nextArmWindow, Number(payloadWindowOrdinal || 0));
    }
    const eventWindow = payloadWindowOrdinal
      || Number(translatorWindowCounters[eventArm] || 0)
      || Math.max(winCounter, 1);
    const ctxPlan = stagePlan.find(s => s.id === stage);
    const ctx = {
      label: ctxPlan ? ctxPlan.label : stage,
      win: event.startsWith("window") || ["prompt_built", "request_sent", "response_received", "json_parsed", "persist_buffered"].includes(event) ? eventWindow : null,
    };
    const severity = consoleEventSeverity(raw);
    if (severity === "warning") warnings += 1;
    if (severity === "error") errors += 1;

    if (stageInfo[stage]) {
      const si = stageInfo[stage];
      if (event === "stage_start" || event === "stage_started" || event === "stage_progress") { si.status = "active"; si.start = si.start || raw.ts; }
      else if (event === "stage_done" || event === "stage_completed") { si.status = event === "stage_done" && payload.exit_code !== 0 ? "failed" : "done"; si.end = raw.ts; si.exit = payload.exit_code ?? null; }
      else if (event === "stage_failed") { si.status = "failed"; si.end = raw.ts; }
      else if (event === "stage_skipped") {
        si.status = "done";
        si.skipped = true;
        si.end = raw.ts;
        si.skipReason = String(payload.reason || "");
      }
      else if (event === "window_preview_available") { si.previews += 1; }
      if (severity === "error") si.status = "failed";
    }
    const progressTotal = payload.progress && payload.progress.total != null ? Number(payload.progress.total) : null;
    const payloadTotal = payload.total_windows ?? payload.windows_total ?? payload.window_total ?? progressTotal;
    if (stage === "translator" && payloadTotal != null && Number.isFinite(Number(payloadTotal))) {
      translatorTotal = Math.max(Number(translatorTotal || 0), Number(payloadTotal));
    }
    const payloadArmTotal = payload.arm_total_windows ?? payload.config_total_windows;
    if (stage === "translator" && payloadArmTotal != null && Number.isFinite(Number(payloadArmTotal))) {
      translatorTotalsByArm[eventArm] = Math.max(Number(translatorTotalsByArm[eventArm] || 0), Number(payloadArmTotal));
    }
    if (event === "window_preview_available") {
      const previewWindow = Math.max(Number(eventWindow || 0), 1);
      previewWindowsByArm[eventArm] = Math.max(Number(previewWindowsByArm[eventArm] || 0), previewWindow);
      latestPreviewWin = previewWindow;
      latestPreviewArm = eventArm;
    }
    if ((event === "prompt_built" || event === "pack_built") && payload.pack_summary) {
      latestPackSummary = payload.pack_summary;
      latestPackWindow = payload.window_id || (ctx.win ? `window ${ctx.win}` : "");
    }
    if (event === "checkpoint" && payload.checkpoint === "phase_1_done") phase1Done = true;
    if (event === "cost_snapshot") {
      if (payload.estimated_cumulative_usd != null) cumulativeCost = Number(payload.estimated_cumulative_usd);
      if (payload.budget_cap_usd != null) budgetCap = Number(payload.budget_cap_usd);
    }
    if (event === "gate_pause" || event === "component_halted") {
      paused = true;
      pausedReason = payload.reason || payload.checkpoint || "checkpoint";
    } else if (paused && (event === "stage_start" || event === "stage_started" || event === "component_resumed")) {
      paused = false;
      pausedReason = "";
    }
    if (event === "artifact_created" && (payload.artifact_ref || payload.artifact_path)) latestArtifact = payload.artifact_ref || payload.artifact_path;
    if ((event === "stage_done" || event === "stage_completed") && (payload.artifact_ref || payload.artifact_path)) latestArtifact = payload.artifact_ref || payload.artifact_path;
    if (event === "health_check") {
      const id = String(payload.id || "unknown_check").trim() || "unknown_check";
      systemChecks[id] = {
        id,
        ok: payload.ok === true ? true : payload.ok === false ? false : null,
        ts: raw.ts || "",
        endpoint: String(payload.endpoint || ""),
        expectedModel: String(payload.expected_model || ""),
        matchedModels: Array.isArray(payload.matched_models) ? payload.matched_models.map(String) : [],
        module: String(payload.module || ""),
        python: String(payload.python || ""),
      };
    }
    if (event === "memory_delta") {
      memoryDelta = consoleMemoryDelta(raw, payload);
      if (memoryDelta) {
        if (!memoryDeltaIds.has(memoryDelta.deltaId)) {
          memoryDeltaIds.add(memoryDelta.deltaId);
          memoryDeltas.push(memoryDelta);
        }
      } else invalidMemoryDeltaCount += 1;
    }
    if (event === "run_done") runStatus = payload.status || "done";
    if (event === "run_failed") { runStatus = "failed"; if (payload.error) stderrTail = String(payload.error).split("\n").slice(-4); }
    if (event === "component_failed") { runStatus = "failed"; if (payload.error) stderrTail = String(payload.error).split("\n").slice(-4); }
    if (event === "error" && payload.error) stderrTail = String(payload.error).split("\n").slice(-4);

    const message = consoleMessageFor(raw, ctx);
    normalized.push({
      key: raw.event_id || `${raw.seq}:${idx}`,
      ts: raw.ts || "",
      stage,
      agent: raw.agent || "",
      event,
      severity,
      seq: raw.seq,
      attempt: raw.attempt_id ?? raw.component?.component_attempt_id ?? null,
      attemptIndex: raw.attempt_index ?? raw.component?.component_attempt_index ?? null,
      lineNo: idx + 1,
      glyph: CONSOLE_SEVERITY_GLYPH[severity === "info" && event === "cost_snapshot" ? "context" : severity] || "├",
      isCost: event === "cost_snapshot",
      isContext: event === "gate_pause",
      dur: payload.duration_s || payload.latency_s || null,
      message,
      heartbeatPid: event === "heartbeat" ? payload.active_child_pid : null,
      heartbeatBaseMessage: event === "heartbeat" ? message : "",
      skipReason: event === "stage_skipped" ? String(payload.reason || "") : "",
      blockId: String(payload.block_id || ""),
      artifactPath: String(payload.artifact_ref || payload.artifact_path || ""),
      memoryDeltaId: memoryDelta ? memoryDelta.deltaId : "",
      rawEventCount: 1,
    });
  });

  const stagesSeen = stagePlan.filter(s => stageInfo[s.id].status !== "pending").length;
  const lastTs = normalized.length ? normalized[normalized.length - 1].ts : "";
  // llm-ish activity from the translator lifecycle (real runs have no llm_call yet)
  const llmCalls = normalized.filter(r => r.event === "request_sent" || r.event === "llm_call").length;

  return {
    normalized, stageInfo, stagesSeen, cumulativeCost, budgetCap,
    warnings, errors, phase1Done, runStatus, latestArtifact, latestPreviewWin, latestPreviewArm,
    stderrTail, paused, pausedReason, lastTs, llmCalls,
    translatorTotal, translatorTotalsByArm, previewWindowsByArm, latestPackSummary, latestPackWindow,
    memoryDeltas, invalidMemoryDeltaCount,
    systemChecks: consoleSystemChecks(systemChecks),
    totalEvents: normalized.length,
  };
}

function consoleCurrentMemoryRows(deltas) {
  const rows = new Map();
  deltas.forEach(delta => {
    const key = `${delta.domain}:${delta.collection}:${delta.recordId}`;
    rows.set(key, delta);
  });
  return Array.from(rows.values()).sort((a, b) =>
    String(a.domain).localeCompare(String(b.domain))
    || String(a.collection).localeCompare(String(b.collection))
    || String(a.label).localeCompare(String(b.label)));
}

function consolePackInjectedCount(summary) {
  if (!summary || typeof summary !== "object") return null;
  const direct = summary.injected ?? summary.injected_count ?? summary.included ?? summary.included_count;
  if (direct != null && Number.isFinite(Number(direct))) return Number(direct);
  const buckets = ["mandatory", "soft", "preserve", "address", "quarantine"];
  const values = buckets.map(key => Number(summary[key])).filter(Number.isFinite);
  return values.length ? values.reduce((sum, value) => sum + value, 0) : null;
}

function consoleCompactConsistency(tierRows) {
  let terms = 0;
  let consistent = 0;
  tierRows.forEach(row => {
    const selected = [row.s1, row.fallback, row.s0]
      .find(item => item && Number(item.terms || 0) > 0);
    if (!selected) return;
    terms += Number(selected.terms || 0);
    consistent += Number(selected.consistent_terms || 0);
  });
  return terms ? { terms, consistent, drift: Math.max(0, terms - consistent) } : null;
}

function consolePreviewWindowOrdinal(windowId) {
  const match = String(windowId || "").match(/(\d+)$/);
  return match ? Number(match[1]) : null;
}

function consolePreviewBlockOrdinal(blockId) {
  const match = String(blockId || "").match(/(?:^|_)b(\d+)$/i);
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
}

function consolePreviewArm(row) {
  return String(row && (row.config || row.arm || row.variant) || "translation");
}

function consolePreviewAbsoluteOrder(row) {
  const value = row && (row.order_index ?? row.block_order ?? row.source_order);
  return value != null && Number.isFinite(Number(value)) ? Number(value) : null;
}

function consolePreviewSourceFlows(row) {
  const blockType = String(row && (row.block_type || row.source_block_type) || "").trim().toLowerCase();
  if (blockType) return blockType === "prose" || blockType === "paragraph";
  const source = String(row && row.source_text || "");
  if (/^(?: {4}|\t)/u.test(source)) return false;
  const leading = source.trimStart();
  return !/^(?:#{1,6}\s|```|~~~|\$\$|\\\[|[-*+]\s|\d+[.)]\s|\|)/u.test(leading);
}

function consolePreviewArmTotals(rows) {
  const totals = {};
  (Array.isArray(rows) ? rows : []).forEach(row => {
    if (!row || typeof row !== "object") return;
    const arm = consolePreviewArm(row);
    const ordinal = consolePreviewWindowOrdinal(row.window_id);
    if (ordinal != null) totals[arm] = Math.max(Number(totals[arm] || 0), ordinal);
  });
  return totals;
}

function consolePreviewProgressLabel(progressByArm, totalsByArm) {
  const progress = progressByArm && typeof progressByArm === "object" ? progressByArm : {};
  const totals = totalsByArm && typeof totalsByArm === "object" ? totalsByArm : {};
  const arms = Array.from(new Set([...Object.keys(totals), ...Object.keys(progress)]))
    .filter(Boolean)
    .sort((a, b) => String(a).localeCompare(String(b), undefined, { numeric: true }));
  return arms.map(arm => {
    const current = Number(progress[arm] || 0);
    const total = Number(totals[arm] || 0);
    return `${arm} ${formatConsoleInt(current)}${total ? `/${formatConsoleInt(total)}` : ""}`;
  }).join(" · ");
}

function consolePreviewRowsThroughProgress(rows, progressByArm) {
  const safeRows = Array.isArray(rows) ? rows.filter(row => row && typeof row === "object") : [];
  const progress = progressByArm && typeof progressByArm === "object" ? progressByArm : {};
  if (!safeRows.length || !Object.keys(progress).length) return [];
  const explicitArms = Object.keys(progress).filter(arm => arm !== "translation");
  const visibleRows = safeRows.filter(row => {
    const arm = consolePreviewArm(row);
    const cursor = progress[arm] != null
      ? Number(progress[arm])
      : explicitArms.length === 0 && progress.translation != null
        ? Number(progress.translation)
        : 0;
    if (!cursor) return false;
    const rowWindow = consolePreviewWindowOrdinal(row.window_id);
    return rowWindow == null || rowWindow <= cursor;
  });
  const deduped = new Map();
  visibleRows.forEach((row, index) => {
    const stableId = row.block_id || `${row.window_id || "window"}:${index}`;
    deduped.set(`${consolePreviewArm(row)}:${stableId}`, row);
  });
  return Array.from(deduped.values()).sort((a, b) => {
    const absoluteA = consolePreviewAbsoluteOrder(a);
    const absoluteB = consolePreviewAbsoluteOrder(b);
    if (absoluteA != null || absoluteB != null) {
      if (absoluteA == null) return 1;
      if (absoluteB == null) return -1;
      if (absoluteA !== absoluteB) return absoluteA - absoluteB;
    }
    return Number(consolePreviewWindowOrdinal(a.window_id) || 0) - Number(consolePreviewWindowOrdinal(b.window_id) || 0)
      || consolePreviewBlockOrdinal(a.block_id) - consolePreviewBlockOrdinal(b.block_id)
      || String(a.block_id || "").localeCompare(String(b.block_id || ""));
  });
}

function consolePreviewComparisonArms(arms) {
  const rank = arm => {
    const value = String(arm || "").toUpperCase();
    if (value === "S0") return 0;
    if (value === "S1") return 1;
    return 2;
  };
  return Array.from(new Set((Array.isArray(arms) ? arms : []).filter(Boolean)))
    .sort((a, b) => rank(a) - rank(b)
      || String(a).localeCompare(String(b), undefined, { numeric: true }))
    .slice(0, 2);
}

function consolePreviewComparisonRows(rows, arms) {
  const comparisonArms = consolePreviewComparisonArms(arms);
  const groups = new Map();
  (Array.isArray(rows) ? rows : []).forEach((row, index) => {
    if (!row || typeof row !== "object") return;
    const arm = consolePreviewArm(row);
    if (!comparisonArms.includes(arm)) return;
    const stableId = String(row.block_id || `${row.window_id || "window"}:${index}`);
    const current = groups.get(stableId) || {
      block_id: row.block_id || stableId,
      source_text: row.source_text || "",
      sourceFlows: consolePreviewSourceFlows(row),
      absoluteOrder: consolePreviewAbsoluteOrder(row),
      blockOrder: consolePreviewBlockOrdinal(row.block_id),
      rowsByArm: {},
    };
    current.rowsByArm[arm] = row;
    if (!current.source_text && row.source_text) {
      current.source_text = row.source_text;
      current.sourceFlows = consolePreviewSourceFlows(row);
    }
    const absoluteOrder = consolePreviewAbsoluteOrder(row);
    if (absoluteOrder != null && (current.absoluteOrder == null || absoluteOrder < current.absoluteOrder)) {
      current.absoluteOrder = absoluteOrder;
    }
    groups.set(stableId, current);
  });
  return Array.from(groups.values()).sort((a, b) => {
    if (a.absoluteOrder != null || b.absoluteOrder != null) {
      if (a.absoluteOrder == null) return 1;
      if (b.absoluteOrder == null) return -1;
      if (a.absoluteOrder !== b.absoluteOrder) return a.absoluteOrder - b.absoluteOrder;
    }
    return a.blockOrder - b.blockOrder
      || String(a.block_id || "").localeCompare(String(b.block_id || ""));
  });
}

function consoleLatestPreviewUpdate(rows, arm, progressByArm = {}) {
  const selectedArm = String(arm || "");
  if (!selectedArm) return null;
  const candidates = (Array.isArray(rows) ? rows : [])
    .filter(row => consolePreviewArm(row) === selectedArm);
  if (!candidates.length) return null;
  const currentWindow = Number(progressByArm[selectedArm] || 0);
  const currentRows = currentWindow > 0
    ? candidates.filter(row => Number(consolePreviewWindowOrdinal(row.window_id) || 0) === currentWindow)
    : [];
  const pool = currentRows.length ? currentRows : candidates;
  return pool[pool.length - 1] || null;
}

function consolePreviewRecordKey(row, index = 0) {
  if (!row || typeof row !== "object") return `preview:${index}`;
  return [
    consolePreviewArm(row) || "translation",
    row.window_id || "window",
    row.block_id || `block:${index}`,
  ].join(":");
}

function consolePreviewPresentationDelay(pace, backlog) {
  if (pace === "instant") return 0;
  if (pace === "slow") return 560;
  if (pace === "fast") return 90;
  if (backlog > 16) return 40;
  if (backlog > 8) return 80;
  if (backlog > 4) return 150;
  return 280;
}

function ConsoleMemoryLedger({
  deltas = [],
  invalidCount = 0,
  watchlist = [],
  packSummary = null,
  packWindow = "",
  consistencyTierRows = [],
  runKey = "",
  previews = [],
  previewAvailable = false,
  previewProgress = {},
  previewTotals = {},
  latestPreviewArm = "",
  centerMode = "split",
  onCenterMode,
  layoutPreferences = CONSOLE_LAYOUT_DEFAULTS,
  onLayoutPreferences,
  navigationTarget = null,
  onNavigationResult,
}) {
  const [open, setOpen] = React.useState(layoutPreferences.ledgerOpen !== false);
  const [surface, setSurface] = React.useState(layoutPreferences.ledgerSurface || "translation");
  const [view, setView] = React.useState(layoutPreferences.ledgerView || "changes");
  const [collectionFilter, setCollectionFilter] = React.useState("all");
  const [translationLayout, setTranslationLayout] = React.useState(layoutPreferences.translationLayout || "target");
  const [translationArm, setTranslationArm] = React.useState(latestPreviewArm || "");
  const [followTail, setFollowTail] = React.useState(true);
  const [focusedBlockId, setFocusedBlockId] = React.useState("");
  const [pendingPreviewCount, setPendingPreviewCount] = React.useState(0);
  const [narrowTranslationView, setNarrowTranslationView] = React.useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(max-width: 1180px)").matches
      : false);
  const [presentationPace, setPresentationPace] = React.useState("adaptive");
  const [presentedPreviewKeys, setPresentedPreviewKeys] = React.useState(() => new Set());
  const [arrivingPreviewKeys, setArrivingPreviewKeys] = React.useState(() => new Set());
  const [lastPresentedKey, setLastPresentedKey] = React.useState("");
  const [presentationBacklog, setPresentationBacklog] = React.useState(0);
  const [selectedBlockId, setSelectedBlockId] = React.useState("");
  const [selectedDeltaId, setSelectedDeltaId] = React.useState("");
  const translationFeedRef = React.useRef(null);
  const memoryFeedRef = React.useRef(null);
  const pendingPreviewTargetRef = React.useRef(null);
  const pendingMemoryTargetRef = React.useRef("");
  const lastNavigationTokenRef = React.useRef(null);
  const programmaticPreviewScrollRef = React.useRef(false);
  const presentationInitializedRef = React.useRef(false);
  const presentationRunRef = React.useRef(runKey);
  const presentationKnownKeysRef = React.useRef(new Set());
  const presentationQueueRef = React.useRef([]);
  const presentationTimerRef = React.useRef(null);
  const arrivalTimersRef = React.useRef(new Map());
  const sawDeltaRef = React.useRef(deltas.length > 0);
  const sawPreviewRef = React.useRef(previewAvailable);
  const runKeyRef = React.useRef(runKey);
  const latestPreviewArmRef = React.useRef(latestPreviewArm);
  React.useEffect(() => {
    setOpen(layoutPreferences.ledgerOpen !== false);
  }, [layoutPreferences.ledgerOpen]);
  React.useEffect(() => {
    setSurface(layoutPreferences.ledgerSurface || "translation");
  }, [layoutPreferences.ledgerSurface]);
  React.useEffect(() => {
    setView(layoutPreferences.ledgerView || "changes");
  }, [layoutPreferences.ledgerView]);
  React.useEffect(() => {
    setTranslationLayout(layoutPreferences.translationLayout || "target");
  }, [layoutPreferences.translationLayout]);
  React.useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const query = window.matchMedia("(max-width: 1180px)");
    const sync = event => setNarrowTranslationView(Boolean(event.matches));
    setNarrowTranslationView(query.matches);
    if (typeof query.addEventListener === "function") query.addEventListener("change", sync);
    else query.addListener(sync);
    return () => {
      if (typeof query.removeEventListener === "function") query.removeEventListener("change", sync);
      else query.removeListener(sync);
    };
  }, []);

  function updateLedgerPreference(key, value) {
    onLayoutPreferences?.({ [key]: value });
  }

  function chooseLedgerSurface(nextSurface) {
    setSurface(nextSurface);
    updateLedgerPreference("ledgerSurface", nextSurface);
  }

  function chooseLedgerView(nextView) {
    setView(nextView);
    updateLedgerPreference("ledgerView", nextView);
  }

  function chooseTranslationLayout(nextLayout) {
    setTranslationLayout(nextLayout);
    updateLedgerPreference("translationLayout", nextLayout);
  }

  function toggleLedgerOpen() {
    setOpen(current => {
      const next = !current;
      updateLedgerPreference("ledgerOpen", next);
      return next;
    });
  }
  React.useEffect(() => {
    if (!sawDeltaRef.current && deltas.length > 0) {
      sawDeltaRef.current = true;
      setOpen(true);
    }
  }, [deltas.length]);
  React.useEffect(() => {
    const runChanged = runKeyRef.current !== runKey;
    if (runChanged) {
      runKeyRef.current = runKey;
      latestPreviewArmRef.current = latestPreviewArm;
      sawDeltaRef.current = deltas.length > 0;
      sawPreviewRef.current = previewAvailable;
      setOpen(layoutPreferences.ledgerOpen !== false);
      setSurface(previewAvailable && layoutPreferences.ledgerSurface === "translation" ? "translation" : "memory");
      setTranslationArm(latestPreviewArm || "");
      setFocusedBlockId("");
      setSelectedBlockId("");
      setSelectedDeltaId("");
      lastNavigationTokenRef.current = null;
      return;
    }
    if (!previewAvailable) {
      sawPreviewRef.current = false;
      setSurface("memory");
      return;
    }
    if (!sawPreviewRef.current) {
      sawPreviewRef.current = true;
      if (layoutPreferences.ledgerSurface === "translation") setSurface("translation");
      setOpen(true);
    }
  }, [previewAvailable, runKey, layoutPreferences.ledgerOpen, layoutPreferences.ledgerSurface]);
  const counts = React.useMemo(() => {
    const next = { all: deltas.length };
    CONSOLE_MEMORY_COLLECTIONS.forEach(key => { next[key] = 0; });
    CONSOLE_MEMORY_OPERATIONS.forEach(key => { next[key] = 0; });
    deltas.forEach(delta => {
      next[delta.collection] = Number(next[delta.collection] || 0) + 1;
      next[delta.operation] = Number(next[delta.operation] || 0) + 1;
    });
    return next;
  }, [deltas]);
  const visibleChanges = React.useMemo(() => {
    const matching = deltas.filter(delta => collectionFilter === "all" || delta.collection === collectionFilter);
    const tail = matching.slice(-120);
    if (selectedDeltaId && !tail.some(delta => delta.deltaId === selectedDeltaId)) {
      const selected = matching.find(delta => delta.deltaId === selectedDeltaId);
      if (selected) tail.push(selected);
    }
    return tail.reverse();
  }, [deltas, collectionFilter, selectedDeltaId]);
  const currentRows = React.useMemo(() => consoleCurrentMemoryRows(deltas), [deltas]);
  const currentCounts = React.useMemo(() => {
    const next = { all: currentRows.length, term: 0, entity: 0 };
    currentRows.forEach(row => { next[row.collection] += 1; });
    return next;
  }, [currentRows]);
  const visibleCurrent = React.useMemo(() => currentRows
    .filter(row => collectionFilter === "all" || row.collection === collectionFilter), [currentRows, collectionFilter]);
  const filterCounts = view === "current" ? currentCounts : counts;
  const packCount = consolePackInjectedCount(packSummary);
  const consistency = consoleCompactConsistency(consistencyTierRows);
  const previewArms = React.useMemo(() => consolePreviewComparisonArms([
    ...Object.keys(previewTotals || {}),
    ...previews.map(consolePreviewArm),
  ]), [previews, previewTotals]);
  React.useEffect(() => {
    if (!previewArms.length) {
      setTranslationArm("");
      return;
    }
    if (!previewArms.includes(translationArm)) {
      setTranslationArm(previewArms[0]);
    }
  }, [previewArms.join("|"), translationArm]);
  React.useEffect(() => {
    if (!latestPreviewArm || latestPreviewArmRef.current === latestPreviewArm) return;
    latestPreviewArmRef.current = latestPreviewArm;
  }, [latestPreviewArm]);
  const previewSignature = React.useMemo(() => previews
    .map((row, index) => consolePreviewRecordKey(row, index))
    .join("|"), [previews]);
  const presentedPreviews = React.useMemo(() => previews.filter((row, index) =>
    presentedPreviewKeys.has(consolePreviewRecordKey(row, index))),
  [previews, presentedPreviewKeys]);
  const visiblePreviews = React.useMemo(() => presentedPreviews.filter(row =>
    !translationArm || consolePreviewArm(row) === translationArm), [presentedPreviews, translationArm]);
  const receivedVisiblePreviews = React.useMemo(() => previews.filter(row =>
    !translationArm || consolePreviewArm(row) === translationArm), [previews, translationArm]);
  const comparisonRows = React.useMemo(() =>
    consolePreviewComparisonRows(presentedPreviews, previewArms), [presentedPreviews, previewArms.join("|")]);
  const receivedComparisonRows = React.useMemo(() =>
    consolePreviewComparisonRows(previews, previewArms), [previews, previewArms.join("|")]);
  const effectiveTranslationLayout = translationLayout === "triple"
    && (previewArms.length < 2 || narrowTranslationView)
    ? "parallel"
    : translationLayout;
  const tripleLayout = effectiveTranslationLayout === "triple" && previewArms.length >= 2;
  const translationBlockCount = tripleLayout ? comparisonRows.length : visiblePreviews.length;
  const translationReceivedBlockCount = tripleLayout ? receivedComparisonRows.length : receivedVisiblePreviews.length;
  const focusedPreviewGroup = React.useMemo(() => {
    if (!focusedBlockId) return null;
    return receivedComparisonRows.find(group =>
      String(group.block_id || "") === String(focusedBlockId)) || null;
  }, [focusedBlockId, receivedComparisonRows]);
  const focusedReadyArms = focusedPreviewGroup
    ? consolePreviewComparisonArms(previewArms).filter(arm => focusedPreviewGroup.rowsByArm[arm])
    : [];
  const lastPresentedPreview = React.useMemo(() => previews.find((row, index) =>
    consolePreviewRecordKey(row, index) === lastPresentedKey) || null,
  [previews, lastPresentedKey]);
  const activeLatestArm = lastPresentedPreview
    ? consolePreviewArm(lastPresentedPreview)
    : translationArm || previewArms[0] || "";
  const latestPreviewUpdate = lastPresentedPreview || consoleLatestPreviewUpdate(presentedPreviews, activeLatestArm, {});
  const latestSelectedPreview = React.useMemo(() =>
    consoleLatestPreviewUpdate(presentedPreviews, translationArm, {}),
  [presentedPreviews, translationArm]);
  const latestPreview = tripleLayout ? latestPreviewUpdate : latestSelectedPreview;
  // Switching from S0 to S1 is presentation progress, not a new stream.
  // Keep the reader's follow choice stable while the queue crosses arms.
  const previewStreamKey = runKey;
  const previewProgressText = consolePreviewProgressLabel(previewProgress, previewTotals);
  const selectedPreviewWindow = Number(previewProgress[translationArm] || 0);
  const selectedPreviewTotal = Number(previewTotals[translationArm] || 0);

  function scrollTranslationToEnd() {
    const feed = translationFeedRef.current;
    if (!feed) return;
    feed.scrollTop = feed.scrollHeight;
  }

  function scrollTranslationToPreview(row) {
    const feed = translationFeedRef.current;
    if (!feed || !row || !row.block_id) {
      scrollTranslationToEnd();
      return;
    }
    const target = Array.from(feed.querySelectorAll("[data-preview-block-id]"))
      .find(node => node.dataset.previewBlockId === String(row.block_id));
    if (!target) {
      scrollTranslationToEnd();
      return;
    }
    const feedRect = feed.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const targetTop = feed.scrollTop + targetRect.top - feedRect.top;
    const bottomPadding = 12;
    feed.scrollTop = Math.max(
      0,
      targetTop - Math.max(bottomPadding, feed.clientHeight - targetRect.height - bottomPadding),
    );
  }

  function queueTranslationScroll(row) {
    programmaticPreviewScrollRef.current = true;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        scrollTranslationToPreview(row);
        requestAnimationFrame(() => {
          programmaticPreviewScrollRef.current = false;
        });
      });
    });
  }

  function scrollMemoryToDelta(deltaId) {
    const feed = memoryFeedRef.current;
    if (!feed || !deltaId) return;
    const target = Array.from(feed.querySelectorAll("[data-memory-delta-id]"))
      .find(node => node.dataset.memoryDeltaId === String(deltaId));
    if (!target) return;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function queueMemoryScroll(deltaId) {
    pendingMemoryTargetRef.current = deltaId;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (pendingMemoryTargetRef.current !== deltaId) return;
        scrollMemoryToDelta(deltaId);
        pendingMemoryTargetRef.current = "";
      });
    });
  }

  function focusTranslationBlock(blockId) {
    if (!blockId) return;
    setFocusedBlockId(String(blockId));
    setSelectedBlockId(String(blockId));
    setFollowTail(false);
  }

  function clearTranslationFocus() {
    setFocusedBlockId("");
    setSelectedBlockId("");
  }

  React.useEffect(() => {
    if (!focusedBlockId) return undefined;
    const handleKeyDown = event => {
      if (event.key === "Escape") clearTranslationFocus();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [focusedBlockId]);

  React.useEffect(() => {
    if (!navigationTarget || navigationTarget.token == null) return;
    if (lastNavigationTokenRef.current === navigationTarget.token) return;
    lastNavigationTokenRef.current = navigationTarget.token;

    if (navigationTarget.kind === "block") {
      const row = previews.find(preview => String(preview.block_id || "") === String(navigationTarget.id || ""));
      if (!row) {
        onNavigationResult?.({ ok: false, kind: "block", id: navigationTarget.id, reason: "not_visible" });
        return;
      }
      setOpen(true);
      chooseLedgerSurface("translation");
      setPresentedPreviewKeys(current => {
        const next = new Set(current);
        previews.forEach((preview, index) => {
          if (String(preview.block_id || "") === String(navigationTarget.id || "")) {
            next.add(consolePreviewRecordKey(preview, index));
          }
        });
        return next;
      });
      const arm = consolePreviewArm(row);
      if (arm && effectiveTranslationLayout !== "triple") setTranslationArm(arm);
      focusTranslationBlock(navigationTarget.id);
      onNavigationResult?.({ ok: true, kind: "block", id: navigationTarget.id });
      return;
    }

    if (navigationTarget.kind === "memory") {
      const delta = deltas.find(item => item.deltaId === navigationTarget.id);
      if (!delta) {
        onNavigationResult?.({ ok: false, kind: "memory", id: navigationTarget.id, reason: "not_visible" });
        return;
      }
      setOpen(true);
      chooseLedgerSurface("memory");
      chooseLedgerView("changes");
      setCollectionFilter(delta.collection || "all");
      setSelectedDeltaId(delta.deltaId);
      queueMemoryScroll(delta.deltaId);
      onNavigationResult?.({ ok: true, kind: "memory", id: delta.deltaId });
    }
  }, [navigationTarget?.token, previews, deltas, effectiveTranslationLayout]);

  React.useEffect(() => {
    if (navigationTarget) return;
    setSelectedBlockId("");
    setSelectedDeltaId("");
  }, [navigationTarget]);

  function resumeTranslationFollow() {
    const target = latestPreview;
    if (!tripleLayout && activeLatestArm && previewArms.includes(activeLatestArm)) {
      setTranslationArm(activeLatestArm);
    }
    pendingPreviewTargetRef.current = null;
    clearTranslationFocus();
    setFollowTail(true);
    setPendingPreviewCount(0);
    if (!presentationBacklog) queueTranslationScroll(target);
  }

  function handleTranslationScroll(event) {
    if (programmaticPreviewScrollRef.current) return;
    if (focusedBlockId) return;
    const feed = event.currentTarget;
    const atTail = feed.scrollHeight - feed.scrollTop - feed.clientHeight <= 40;
    if (atTail) {
      setFollowTail(true);
      setPendingPreviewCount(0);
      pendingPreviewTargetRef.current = null;
    } else {
      setFollowTail(false);
    }
  }

  React.useEffect(() => {
    const rows = previews.map((row, index) => ({
      key: consolePreviewRecordKey(row, index),
      row,
      arm: consolePreviewArm(row),
    }));
    const currentKeys = new Set(rows.map(item => item.key));
    const runChanged = presentationRunRef.current !== runKey;

    function resetPresentation(seedRows) {
      if (presentationTimerRef.current) {
        clearTimeout(presentationTimerRef.current);
        presentationTimerRef.current = null;
      }
      arrivalTimersRef.current.forEach(timer => clearTimeout(timer));
      arrivalTimersRef.current.clear();
      presentationQueueRef.current = [];
      presentationKnownKeysRef.current = new Set(seedRows.map(item => item.key));
      setPresentedPreviewKeys(new Set(seedRows.map(item => item.key)));
      setArrivingPreviewKeys(new Set());
      setLastPresentedKey(seedRows.length ? seedRows[seedRows.length - 1].key : "");
      setPresentationBacklog(0);
      setPendingPreviewCount(0);
      pendingPreviewTargetRef.current = null;
    }

    if (!presentationInitializedRef.current || runChanged) {
      presentationInitializedRef.current = true;
      presentationRunRef.current = runKey;
      resetPresentation(rows);
      return;
    }

    const rewind = Array.from(presentationKnownKeysRef.current)
      .some(key => !currentKeys.has(key));
    if (rewind) {
      resetPresentation(rows);
      return;
    }

    const additions = rows.filter(item => !presentationKnownKeysRef.current.has(item.key));
    if (!additions.length) return;
    additions.forEach(item => presentationKnownKeysRef.current.add(item.key));
    presentationQueueRef.current.push(...additions);
    pendingPreviewTargetRef.current = additions[additions.length - 1].row;
    setPresentationBacklog(presentationQueueRef.current.length);
  }, [runKey, previewSignature]);

  React.useEffect(() => {
    if (presentationTimerRef.current) {
      clearTimeout(presentationTimerRef.current);
      presentationTimerRef.current = null;
    }
    if (!followTail || focusedBlockId || !presentationQueueRef.current.length) return undefined;

    const backlog = presentationQueueRef.current.length;
    const delay = consolePreviewPresentationDelay(presentationPace, backlog);
    presentationTimerRef.current = setTimeout(() => {
      presentationTimerRef.current = null;
      const revealCount = presentationPace === "instant"
        ? presentationQueueRef.current.length
        : 1;
      const revealed = presentationQueueRef.current.splice(0, revealCount);
      if (!revealed.length) {
        setPresentationBacklog(0);
        return;
      }

      const revealedKeys = revealed.map(item => item.key);
      const last = revealed[revealed.length - 1];
      setPresentedPreviewKeys(previous => {
        const next = new Set(previous);
        revealedKeys.forEach(key => next.add(key));
        return next;
      });
      setArrivingPreviewKeys(previous => {
        const next = new Set(previous);
        revealedKeys.forEach(key => next.add(key));
        return next;
      });
      revealedKeys.forEach(key => {
        const existing = arrivalTimersRef.current.get(key);
        if (existing) clearTimeout(existing);
        const timer = setTimeout(() => {
          arrivalTimersRef.current.delete(key);
          setArrivingPreviewKeys(previous => {
            if (!previous.has(key)) return previous;
            const next = new Set(previous);
            next.delete(key);
            return next;
          });
        }, 720);
        arrivalTimersRef.current.set(key, timer);
      });

      setLastPresentedKey(last.key);
      pendingPreviewTargetRef.current = last.row;
      if (!tripleLayout && last.arm && previewArms.includes(last.arm)) {
        setTranslationArm(last.arm);
      }
      setPresentationBacklog(presentationQueueRef.current.length);
      queueTranslationScroll(last.row);
    }, delay);

    return () => {
      if (presentationTimerRef.current) {
        clearTimeout(presentationTimerRef.current);
        presentationTimerRef.current = null;
      }
    };
  }, [
    followTail,
    presentationBacklog,
    presentationPace,
    tripleLayout,
    focusedBlockId,
    previewArms.join("|"),
  ]);

  React.useEffect(() => {
    setPendingPreviewCount(followTail && !focusedBlockId ? 0 : presentationBacklog);
  }, [followTail, focusedBlockId, presentationBacklog]);

  React.useEffect(() => () => {
    if (presentationTimerRef.current) clearTimeout(presentationTimerRef.current);
    arrivalTimersRef.current.forEach(timer => clearTimeout(timer));
    arrivalTimersRef.current.clear();
  }, []);

  React.useEffect(() => {
    pendingPreviewTargetRef.current = null;
    setFocusedBlockId("");
    setSelectedBlockId("");
    setFollowTail(true);
    setPendingPreviewCount(0);
    if (!presentationBacklog) queueTranslationScroll(latestPreview);
  }, [previewStreamKey]);

  React.useEffect(() => {
    if (open && surface === "translation" && followTail && !focusedBlockId && !presentationBacklog) {
      queueTranslationScroll(latestPreview);
    }
  }, [open, surface, followTail, focusedBlockId]);

  const tabContract = view === "changes"
    ? "memory_delta_v1 · committed only"
    : view === "current"
      ? "run-local projection · not full registry"
      : "watchlist · not committed";

  return (
    <section className={"memory-delta-pane" + (open ? "" : " memory-delta-collapsed")} aria-label={uiText("Sổ cái bộ nhớ", "Memory ledger")}>
      <div className="memory-delta-head">
        <button
          type="button"
          className="memory-delta-toggle"
          aria-label={open ? uiText("Thu gọn sổ cái bộ nhớ", "Collapse memory ledger") : uiText("Mở rộng sổ cái bộ nhớ", "Expand memory ledger")}
          aria-expanded={open}
          title={open ? uiText("Thu gọn cập nhật bộ nhớ", "Collapse memory updates") : uiText("Mở cập nhật bộ nhớ", "Expand memory updates")}
          onClick={toggleLedgerOpen}
        >
          {open ? "⌄" : "⌃"}
        </button>
        <span className="memory-delta-title">:: {uiText("sổ cái lần chạy", "run ledger")}</span>
        <span className="run-ledger-surfaces" role="tablist" aria-label={uiText("Các bề mặt sổ cái lần chạy", "Run ledger surfaces")}>
          <button
            type="button"
            role="tab"
            aria-selected={surface === "memory"}
            className={"run-ledger-surface" + (surface === "memory" ? " active" : "")}
            onClick={() => chooseLedgerSurface("memory")}
          >
            {uiText("Bộ nhớ", "Memory")}
          </button>
          {previewAvailable && (
            <button
              type="button"
              role="tab"
              aria-selected={surface === "translation"}
              className={"run-ledger-surface" + (surface === "translation" ? " active" : "")}
              onClick={() => chooseLedgerSurface("translation")}
            >
              {uiText("Bản dịch", "Translation")}
            </button>
          )}
        </span>
        <span className="memory-delta-total">
          {surface === "translation"
            ? (previewProgressText ? `${previewProgressText} windows` : "0 windows")
            : uiText(`${formatConsoleInt(deltas.length)} thay đổi đã commit`, `${formatConsoleInt(deltas.length)} committed changes`)}
        </span>
        {onCenterMode && (
          <button
            type="button"
            className="console-pane-mode-button"
            onClick={() => onCenterMode(centerMode === "ledger" ? "split" : "ledger")}
            title={centerMode === "ledger" ? uiText("Khôi phục Event Stream và Run Ledger", "Restore Event Stream and Run Ledger") : uiText("Phóng to Run Ledger", "Maximize Run Ledger")}
            aria-label={centerMode === "ledger" ? uiText("Khôi phục Console chia đôi", "Restore split Console view") : uiText("Phóng to Run Ledger", "Maximize Run Ledger")}
          >
            {centerMode === "ledger" ? "↕" : "□"}
          </button>
        )}
        {surface === "memory" && <span className="memory-ledger-summary" aria-label={uiText("Tóm tắt bộ nhớ lần chạy", "Run memory summary")}>
          {packCount != null && (
            <span className="memory-ledger-chip" title={packWindow ? uiText(`Gói ngữ cảnh mới nhất: ${packWindow}`, `Latest context pack: ${packWindow}`) : uiText("Gói ngữ cảnh mới nhất", "Latest context pack")}>
              pack {formatConsoleInt(packCount)}
            </span>
          )}
          {consistency && (
            <span className={"memory-ledger-chip" + (consistency.drift ? " ledger-chip-warn" : " ledger-chip-good")} title={uiText("Mức tuân thủ thuật ngữ đã render", "Rendered terminology adherence")}>
              adherence {formatConsoleInt(consistency.consistent)}/{formatConsoleInt(consistency.terms)}
            </span>
          )}
          {watchlist.length > 0 && (
            <span className="memory-ledger-chip ledger-chip-warn" title={uiText("Bản ghi chờ hoặc đang giữ; chưa commit", "Pending or held records; not committed")}>
              held {formatConsoleInt(watchlist.length)}
            </span>
          )}
        </span>}
        {surface === "translation" && (
          <span className="memory-ledger-summary" aria-label={uiText("Tóm tắt luồng dịch", "Translation stream summary")}>
            <span className="memory-ledger-chip ledger-chip-good">
              {formatConsoleInt(translationBlockCount)} {uiText("block sẵn sàng", "blocks ready")}
            </span>
            {latestPreview && <span className="memory-ledger-chip">{latestPreview.model || latestPreview.config || "translator"}</span>}
          </span>
        )}
        {surface === "memory" && <span className="memory-delta-stats" aria-label={uiText("Tổng thao tác delta bộ nhớ", "Memory delta operation totals")}>
          {[...CONSOLE_MEMORY_OPERATIONS].filter(operation => counts[operation] > 0).map(operation => {
            const meta = CONSOLE_MEMORY_OPERATION_META[operation];
            return (
              <span className={`memory-delta-stat delta-op-${operation}`} key={operation} title={meta.label}>
                {meta.glyph}{formatConsoleInt(counts[operation] || 0)}
              </span>
            );
          })}
        </span>}
      </div>
      {open && surface === "memory" && (
        <>
          <div className="memory-ledger-tabs" role="tablist" aria-label={uiText("Các chế độ sổ cái bộ nhớ", "Memory ledger views")}>
            {[
              ["changes", uiText("Thay đổi", "Changes"), deltas.length],
              ["current", uiText("Hiện có", "Current"), currentRows.length],
              ["pending", uiText("Chờ xử lý", "Pending"), watchlist.length],
            ].map(([key, label, count]) => (
              <button
                type="button"
                role="tab"
                key={key}
                aria-selected={view === key}
                className={"memory-ledger-tab" + (view === key ? " active" : "")}
                onClick={() => chooseLedgerView(key)}
              >
                {label}<span>{formatConsoleInt(count)}</span>
              </button>
            ))}
          </div>
          <div className="memory-delta-toolbar" role="toolbar" aria-label={uiText("Lọc sổ cái bộ nhớ", "Filter memory ledger")}>
            {view !== "pending" && ["all", "term", "entity"].map(collection => (
              <button
                type="button"
                key={collection}
                className={"memory-delta-filter" + (collectionFilter === collection ? " active" : "")}
                aria-pressed={collectionFilter === collection}
                onClick={() => setCollectionFilter(collection)}
              >
                {collection === "all" ? uiText("Tất cả", "All") : (collection === "term" ? uiText("Thuật ngữ", "Terms") : uiText("Thực thể", "Entities"))}
                <span>{formatConsoleInt(filterCounts[collection] || 0)}</span>
              </button>
            ))}
            {view === "pending" && <span className="memory-ledger-note">{uiText("Chỉ các mục đang giữ lại để xem xét; không phải thay đổi bộ nhớ.", "Only records held for review; these are not memory changes.")}</span>}
            <span className="memory-delta-contract">{tabContract}</span>
          </div>
          <div className="memory-delta-feed" ref={memoryFeedRef}>
            {view === "changes" && (visibleChanges.length ? visibleChanges.map(delta => {
              const meta = CONSOLE_MEMORY_OPERATION_META[delta.operation];
              const projection = delta.before && delta.after
                ? <><span>{delta.before}</span><b aria-hidden="true">→</b><span>{delta.after}</span></>
                : <span>{delta.after || delta.before || delta.reasonCode || "no display projection"}</span>;
              return (
                <div
                  className={`memory-delta-row delta-op-${delta.operation}` + (selectedDeltaId === delta.deltaId ? " memory-targeted" : "")}
                  data-memory-delta-id={delta.deltaId}
                  key={delta.key}
                >
                  <span className="memory-delta-time">{delta.ts ? delta.ts.slice(11, 19) : "--:--:--"}</span>
                  <span className="memory-delta-glyph" title={meta.label}>{meta.glyph}</span>
                  <span className="memory-delta-main">
                    <span className="memory-delta-label">
                      <b>{delta.label}</b>
                      <span>
                        {CONSOLE_MEMORY_DOMAIN_LABELS[delta.domain]} · {CONSOLE_MEMORY_COLLECTION_LABELS[delta.collection]} · {delta.stage || delta.agent || "unknown stage"}
                      </span>
                    </span>
                    <span className="memory-delta-projection">{projection}</span>
                  </span>
                  <span className="memory-delta-meta">
                    <span className={`memory-lifecycle memory-lifecycle-${delta.lifecycle}`}>{delta.lifecycle}</span>
                    <span title={`Revision ${delta.revisionAfter}; state generation ${delta.stateGeneration}`}>
                      r{delta.revisionAfter} · g{delta.stateGeneration}
                    </span>
                    {delta.evidenceDelta != null && delta.evidenceDelta !== 0 && (
                      <span title="Evidence delta">{delta.evidenceDelta > 0 ? "+" : ""}{delta.evidenceDelta} ev</span>
                    )}
                    {delta.sourceRefs.length > 0 && (
                      <span title={delta.sourceRefs.map(ref => [ref.chapterId, ref.blockId].filter(Boolean).join("/")).join(", ")}>
                        {delta.sourceRefs.length} ref
                      </span>
                    )}
                    <span title={`Commit receipt: ${delta.receiptId}`}>receipt</span>
                  </span>
                </div>
              );
            }) : (
              <div className="memory-delta-empty">
                {deltas.length
                  ? uiText("Không có thay đổi thuộc nhóm đang lọc.", "No changes match the active filter.")
                  : uiText("Lần chạy này chưa phát committed memory_delta_v1. Panel chỉ hiện thay đổi đã ghi bền vững.", "This run has not emitted committed memory_delta_v1. The panel shows only durably persisted changes.")}
              </div>
            ))}
            {view === "current" && (visibleCurrent.length ? visibleCurrent.map(delta => (
              <div
                className={"memory-delta-row memory-current-row" + (selectedDeltaId === delta.deltaId ? " memory-targeted" : "")}
                data-memory-delta-id={delta.deltaId}
                key={`current:${delta.domain}:${delta.collection}:${delta.recordId}`}
              >
                <span className="memory-delta-glyph" title={uiText("Revision đã commit mới nhất", "Latest committed revision")}>◆</span>
                <span className="memory-delta-main">
                  <span className="memory-delta-label">
                    <b>{delta.label}</b>
                    <span>{CONSOLE_MEMORY_DOMAIN_LABELS[delta.domain]} · {CONSOLE_MEMORY_COLLECTION_LABELS[delta.collection]}</span>
                  </span>
                  <span className="memory-delta-projection"><span>{delta.after || delta.before || "no display projection"}</span></span>
                </span>
                <span className="memory-delta-meta">
                  <span className="memory-lifecycle memory-lifecycle-committed">{uiText("hiện tại", "current")}</span>
                  <span>r{delta.revisionAfter} · g{delta.stateGeneration}</span>
                  <span title={`Commit receipt: ${delta.receiptId}`}>receipt</span>
                </span>
              </div>
            )) : (
              <div className="memory-delta-empty">
                {deltas.length
                  ? uiText("Không có bản ghi hiện hành thuộc nhóm đang lọc.", "No current records match the active filter.")
                  : uiText("Chưa có committed delta để dựng projection của lần chạy này. Đây không phải toàn bộ registry.", "No committed delta is available to build this run projection. This is not the full registry.")}
              </div>
            ))}
            {view === "pending" && (watchlist.length ? watchlist.map((item, index) => {
              const source = item.term || item.source_term || item.surface || item.source || "record";
              const target = item.vi || item.canonical_target_vi || item.target || item.canonical || "?";
              const candidates = consoleWatchCandidatesLine(item);
              const evidence = consoleWatchEvidenceLine(item);
              return (
                <div className="memory-pending-row" key={item.entry_id || `${source}:${index}`}>
                  <span className="memory-delta-glyph delta-pending-glyph" title={uiText("Chờ hoặc đang giữ", "Pending or held")}>!</span>
                  <span className="memory-delta-main">
                    <span className="memory-delta-label">
                      <b>{source}</b><span>watchlist · {consoleWatchReasonLabel(item)}</span>
                    </span>
                    <span className="memory-delta-projection"><span>{target}</span></span>
                    {(candidates || evidence) && (
                      <span className="memory-pending-detail">
                        {[candidates && `candidates: ${candidates}`, evidence && `evidence: ${evidence}`].filter(Boolean).join(" · ")}
                      </span>
                    )}
                  </span>
                  <span className="memory-delta-meta">
                    <span className="memory-lifecycle memory-lifecycle-candidate">{uiText("đang giữ", "held")}</span>
                    <span>{consoleWatchInjectionLabel(item)}</span>
                  </span>
                </div>
              );
            }) : (
              <div className="memory-delta-empty">{uiText("Không có mục chờ/đang giữ ở thời điểm replay hiện tại.", "No pending/held records at the current replay position.")}</div>
            ))}
            {view === "changes" && invalidCount > 0 && (
              <div className="memory-delta-invalid">{uiText(`Đã bỏ qua ${formatConsoleInt(invalidCount)} sự kiện sai contract.`, `${formatConsoleInt(invalidCount)} contract-invalid events were skipped.`)}</div>
            )}
          </div>
        </>
      )}
      {open && surface === "translation" && (
        <div className="translation-stream-pane" role="tabpanel" aria-label={uiText("Luồng dịch", "Translation stream")}>
          <div className="translation-stream-toolbar" role="toolbar" aria-label={uiText("Điều khiển luồng dịch", "Translation stream controls")}>
            <span className="translation-layout-switch" role="group" aria-label={uiText("Bố cục bản dịch", "Translation layout")}>
              <button
                type="button"
                className={effectiveTranslationLayout === "target" ? "active" : ""}
                aria-pressed={effectiveTranslationLayout === "target"}
                onClick={() => chooseTranslationLayout("target")}
              >{uiText("Đích", "Target")}</button>
              <button
                type="button"
                className={effectiveTranslationLayout === "parallel" ? "active" : ""}
                aria-pressed={effectiveTranslationLayout === "parallel"}
                onClick={() => chooseTranslationLayout("parallel")}
              >{uiText("Song song", "Parallel")}</button>
              {previewArms.length >= 2 && !narrowTranslationView && (
                <button
                  type="button"
                  className={effectiveTranslationLayout === "triple" ? "active" : ""}
                  aria-pressed={effectiveTranslationLayout === "triple"}
                  onClick={() => chooseTranslationLayout("triple")}
                  title={uiText(`So sánh Nguồn + ${previewArms.join(" + ")}`, `Compare Source + ${previewArms.join(" + ")}`)}
                >{uiText("3 cột", "3 columns")}</button>
              )}
            </span>
            {previewArms.length > 1 && !tripleLayout && (
              <select
                className="translation-arm-select"
                aria-label={uiText("Nhánh dịch", "Translation arm")}
                value={translationArm}
                onChange={event => setTranslationArm(event.target.value)}
              >
                {previewArms.map(arm => <option key={arm} value={arm}>{arm}</option>)}
              </select>
            )}
            {tripleLayout && (
              <span className="translation-comparison-arms" aria-label={uiText("Các nhánh so sánh bản dịch", "Translation comparison arms")}>
                {previewArms.join(" + ")}
              </span>
            )}
            <select
              className="translation-pace-select"
              aria-label={uiText("Nhịp hiển thị block dịch", "Translation block display pace")}
              value={presentationPace}
              onChange={event => setPresentationPace(event.target.value)}
              title={uiText("Điều chỉnh nhịp đưa các block đã nhận lên màn hình", "Adjust how quickly received blocks appear")}
            >
              <option value="adaptive">{uiText("Nhịp tự động", "Adaptive")}</option>
              <option value="slow">{uiText("Chậm", "Slow")}</option>
              <option value="fast">{uiText("Nhanh", "Fast")}</option>
              <option value="instant">{uiText("Hiện ngay", "Instant")}</option>
            </select>
            <button
              type="button"
              className={"translation-follow-toggle" + (followTail && !focusedBlockId ? " active" : "")}
              aria-pressed={followTail && !focusedBlockId}
              onClick={() => followTail && !focusedBlockId ? setFollowTail(false) : resumeTranslationFollow()}
              title={followTail && !focusedBlockId ? uiText("Tắt tự cuộn để giữ vị trí đang đọc", "Disable auto-scroll and keep the reading position") : uiText("Theo dõi block mới nhất", "Follow the latest block")}
            >
              <span aria-hidden="true">●</span> {uiText("Theo dõi", "Follow")}
            </button>
            <span className="translation-stream-progress">
              {`${formatConsoleInt(translationBlockCount)}/${formatConsoleInt(translationReceivedBlockCount)} block`}
              {tripleLayout
                ? ` · ${previewProgressText || previewArms.join(" + ")}`
                : ` · ${translationArm || "translator"} ${formatConsoleInt(selectedPreviewWindow)}${selectedPreviewTotal ? `/${formatConsoleInt(selectedPreviewTotal)}` : ""} window`}
              {presentationBacklog > 0 ? uiText(` · ${formatConsoleInt(presentationBacklog)} chờ`, ` · ${formatConsoleInt(presentationBacklog)} pending`) : ""}
            </span>
          </div>
          {focusedBlockId && (
            <div className="translation-focus-bar" role="status">
              <button type="button" className="translation-focus-back" onClick={clearTranslationFocus}>
                ← {uiText("Quay lại luồng", "Back to stream")}
              </button>
              <span><b>{focusedBlockId}</b> · {uiText("đang xem riêng block", "focused block view")}</span>
              <span className="translation-focus-hint">{uiText("Esc để thoát", "Press Esc to exit")}</span>
              {pendingPreviewCount > 0 && (
                <button type="button" className="translation-focus-resume" onClick={resumeTranslationFollow}>
                  {formatConsoleInt(pendingPreviewCount)} {uiText("cập nhật mới · Theo dõi tiếp", "new updates · Resume following")}
                </button>
              )}
            </div>
          )}
          <div
            className="translation-stream-feed"
            ref={translationFeedRef}
            onScroll={handleTranslationScroll}
          >
            {focusedBlockId ? (
              focusedPreviewGroup ? (
                <article
                  className="translation-block-row translation-block-focus translation-targeted"
                  data-preview-block-id={focusedPreviewGroup.block_id || ""}
                  key={`focus:${focusedPreviewGroup.block_id || focusedBlockId}`}
                >
                  <header className="translation-block-head">
                    <span className="translation-stream-id">{focusedPreviewGroup.block_id || focusedBlockId}</span>
                    <span className="translation-block-ready">● {focusedReadyArms.length} {uiText("nhánh sẵn sàng", "arm ready")}</span>
                    <span className="translation-stream-model">
                      {Array.from(new Set(focusedReadyArms.map(arm => {
                        const row = focusedPreviewGroup.rowsByArm[arm];
                        return row?.model || row?.config || arm;
                      }).filter(Boolean))).join(" + ") || "translator"}
                    </span>
                    <button
                      type="button"
                      className="translation-focus-button"
                      onClick={clearTranslationFocus}
                      title={uiText("Quay lại luồng block", "Back to block stream")}
                      aria-label={uiText("Thoát chế độ tập trung block", "Exit focused block")}
                    >×</button>
                  </header>
                  <div className="translation-block-body is-focus">
                    <section className="translation-block-column translation-block-source" aria-label={`Source ${focusedPreviewGroup.block_id || focusedBlockId}`}>
                      <span className="translation-block-label">{uiText("Nguồn", "Source")}</span>
                      <p className={focusedPreviewGroup.sourceFlows ? "translation-source-flow" : ""}>
                        {focusedPreviewGroup.source_text || uiText("Không có source_text trong preview đã lưu.", "No source_text is available in the persisted preview.")}
                      </p>
                    </section>
                    {focusedReadyArms.map(arm => {
                      const row = focusedPreviewGroup.rowsByArm[arm];
                      return (
                        <section
                          className="translation-block-column translation-block-target"
                          aria-label={`${arm} translation ${focusedPreviewGroup.block_id || focusedBlockId}`}
                          key={arm}
                        >
                          <span className="translation-block-label">{uiText("Bản dịch", "Translation")} {arm}</span>
                          <p>{row.target_text || uiText("Không có target_text trong preview đã lưu.", "No target_text is available in the persisted preview.")}</p>
                        </section>
                      );
                    })}
                  </div>
                </article>
              ) : (
                <div className="memory-delta-empty">{uiText("Block đang focus chưa có preview đã lưu.", "The focused block has no persisted preview.")}</div>
              )
            ) : tripleLayout && comparisonRows.length ? comparisonRows.map((group, index) => {
              const isLatest = Boolean(latestPreviewUpdate
                && String(group.block_id || "") === String(latestPreviewUpdate.block_id || ""));
              const readyArms = previewArms.filter(arm => group.rowsByArm[arm]);
              const windows = previewArms.map(arm => {
                const row = group.rowsByArm[arm];
                return row ? `${arm} ${row.window_id || "ready"}` : `${arm} pending`;
              }).join(" · ");
              const models = Array.from(new Set(readyArms.map(arm => {
                const row = group.rowsByArm[arm];
                return row.model || row.config || arm;
              }).filter(Boolean))).join(" + ");
              const arrivingArms = readyArms.filter(arm => {
                const row = group.rowsByArm[arm];
                return row && arrivingPreviewKeys.has(consolePreviewRecordKey(row));
              });
              const isArriving = arrivingArms.length > 0;
              return (
                <article
                  className={"translation-block-row"
                    + (isLatest ? " translation-block-latest" : "")
                    + (isArriving ? " translation-block-arriving" : "")
                    + (selectedBlockId === String(group.block_id || "") ? " translation-targeted" : "")}
                  data-preview-block-id={group.block_id || ""}
                  data-preview-arm={activeLatestArm}
                  key={`triple:${group.block_id || index}`}
                >
                  <header className="translation-block-head">
                    <span className="translation-stream-id">{group.block_id || `block ${index + 1}`}</span>
                    <span>{windows}</span>
                    <span className="translation-block-ready">● {readyArms.length}/{previewArms.length} {uiText("sẵn sàng", "ready")}</span>
                    <span className="translation-stream-model">{models || "translator"}</span>
                    <button
                      type="button"
                      className="translation-focus-button"
                      onClick={() => focusTranslationBlock(group.block_id)}
                      title={uiText("Mở rộng block", "Expand block")}
                      aria-label={uiText(`Tập trung block ${group.block_id || index + 1}`, `Focus block ${group.block_id || index + 1}`)}
                    >□</button>
                  </header>
                  <div className="translation-block-body is-triple">
                    <section className="translation-block-column translation-block-source" aria-label={`Source ${group.block_id || index + 1}`}>
                      <span className="translation-block-label">{uiText("Nguồn", "Source")}</span>
                      <p className={group.sourceFlows ? "translation-source-flow" : ""}>{group.source_text || uiText("Không có source_text trong preview đã lưu.", "No source_text is available in the persisted preview.")}</p>
                    </section>
                    {previewArms.map(arm => {
                      const row = group.rowsByArm[arm];
                      const isColumnArriving = row
                        && arrivingPreviewKeys.has(consolePreviewRecordKey(row));
                      return (
                        <section
                          className={"translation-block-column translation-block-target"
                            + (row ? "" : " translation-block-pending")
                            + (isColumnArriving ? " translation-column-arriving" : "")}
                          aria-label={`${arm} translation ${group.block_id || index + 1}`}
                          key={arm}
                        >
                          <span className="translation-block-label">{uiText("Bản dịch", "Translation")} {arm}</span>
                          <p>{row ? (row.target_text || uiText("Không có target_text trong preview đã lưu.", "No target_text is available in the persisted preview.")) : uiText("Chưa sẵn sàng ở thời điểm replay này.", "Not ready at this replay position.")}</p>
                        </section>
                      );
                    })}
                  </div>
                </article>
              );
            }) : !tripleLayout && visiblePreviews.length ? visiblePreviews.map((row, index) => {
              const isLatest = Boolean(latestSelectedPreview
                && consolePreviewArm(row) === consolePreviewArm(latestSelectedPreview)
                && String(row.block_id || "") === String(latestSelectedPreview.block_id || ""));
              const sourceFlows = consolePreviewSourceFlows(row);
              const rowKey = consolePreviewRecordKey(row, index);
              const isArriving = arrivingPreviewKeys.has(rowKey);
              return (
                <article
                  className={"translation-block-row"
                    + (isLatest ? " translation-block-latest" : "")
                    + (isArriving ? " translation-block-arriving" : "")
                    + (selectedBlockId === String(row.block_id || "") ? " translation-targeted" : "")}
                  data-preview-block-id={row.block_id || ""}
                  data-preview-arm={consolePreviewArm(row)}
                  data-preview-key={rowKey}
                  key={rowKey}
                >
                  <header className="translation-block-head">
                    <span className="translation-stream-id">{row.block_id || `block ${index + 1}`}</span>
                    <span>{row.window_id || "Translator preview"}</span>
                    <span className="translation-block-ready">● {uiText("sẵn sàng", "ready")}</span>
                    <span className="translation-stream-model">{row.model || row.config || "translated"}</span>
                    <button
                      type="button"
                      className="translation-focus-button"
                      onClick={() => focusTranslationBlock(row.block_id)}
                      title={uiText("Mở rộng block", "Expand block")}
                      aria-label={uiText(`Tập trung block ${row.block_id || index + 1}`, `Focus block ${row.block_id || index + 1}`)}
                    >□</button>
                  </header>
                  <div className={"translation-block-body" + (effectiveTranslationLayout === "parallel" ? " is-parallel" : " is-target-only")}>
                    {effectiveTranslationLayout === "parallel" && (
                      <section className="translation-block-column translation-block-source" aria-label={`Source ${row.block_id || index + 1}`}>
                        <span className="translation-block-label">{uiText("Nguồn", "Source")}</span>
                        <p className={sourceFlows ? "translation-source-flow" : ""}>{row.source_text || uiText("Không có source_text trong preview đã lưu.", "No source_text is available in the persisted preview.")}</p>
                      </section>
                    )}
                    <section className="translation-block-column translation-block-target" aria-label={`Translation ${row.block_id || index + 1}`}>
                      <span className="translation-block-label">{uiText("Bản dịch", "Translation")}</span>
                      <p>{row.target_text || uiText("Không có target_text trong preview đã lưu.", "No target_text is available in the persisted preview.")}</p>
                    </section>
                  </div>
                </article>
              );
            }) : (
              <div className="memory-delta-empty">{uiText("Translator đã phát preview, nhưng chưa tải được block đã lưu.", "Translator emitted a preview, but no persisted block could be loaded.")}</div>
            )}
          </div>
          {!focusedBlockId && pendingPreviewCount > 0 && (
            <button type="button" className="translation-new-blocks" onClick={resumeTranslationFollow}>
              {formatConsoleInt(pendingPreviewCount)} {uiText("cập nhật mới · Theo dõi tiếp", "new updates · Resume following")}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function consoleAgeSeconds(ts) {
  if (!ts) return Infinity;
  const t = Date.parse(ts);
  if (isNaN(t)) return Infinity;
  return (Date.now() - t) / 1000;
}

function consoleWorkflowShortHash(value) {
  const text = String(value || "");
  return text ? `${text.slice(0, 10)}…${text.slice(-6)}` : "null";
}

function consoleWorkflowProgress(progress) {
  if (!progress || typeof progress !== "object") return "";
  const completed = progress.completed;
  const total = progress.total;
  if (completed == null && total == null) return "";
  return `${completed ?? "null"}/${total ?? "null"}${progress.unit ? ` ${progress.unit}` : ""}`;
}

function ConsoleWorkflowIdentity({ workflowReplay, uiText }) {
  if (!workflowReplay || !workflowReplay.valid) return null;
  const manifest = workflowReplay.manifest || {};
  const components = Array.isArray(manifest.components) ? manifest.components : [];
  return (
    <section className="workflow-facts" aria-label={uiText("Định danh workflow", "Workflow identity") }>
      <div className="section-label">:: {uiText("workflow", "workflow")}</div>
      <div className="kv-row"><span className="kv-label">workflow_run_id</span><span className="kv-value workflow-mono" title={manifest.workflow_run_id || ""}>{manifest.workflow_run_id || "null"}</span></div>
      <div className="kv-row"><span className="kv-label">job_id</span><span className="kv-value workflow-mono" title={manifest.job_id || ""}>{manifest.job_id || "null"}</span></div>
      <div className="kv-row"><span className="kv-label">status</span><span className="kv-value">{manifest.status ?? "null"}</span></div>
      <div className="kv-row"><span className="kv-label">resume</span><span className="kv-value">{manifest.resume?.available === true ? (manifest.resume.component_id || "true") : manifest.resume?.available === false ? "false" : "null"}</span></div>
      <div className="kv-row"><span className="kv-label">timing</span><span className="kv-value">{manifest.timing_authority ?? "null"}</span></div>
      {manifest.reconstructed === true && <div className="workflow-warning-badge">{uiText("DỰNG LẠI · chỉ có thứ tự logic", "RECONSTRUCTED · logical order only")}</div>}
      <div className="workflow-component-list">
        {components.map(component => (
          <div className="workflow-component-row" key={`${component.component_id}:${component.component_run_id}`}>
            <span>{component.component_id}</span>
            <code title={component.component_run_id}>{component.component_run_id}</code>
            <span className={`workflow-state state-${component.status}`}>{component.status}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function ConsoleWorkflowEvidence({ workflowReplay, uiText, selectedArtifact, onSelectArtifact }) {
  if (!workflowReplay || !workflowReplay.valid) return null;
  const artifacts = workflowReplay.artifacts || [];
  const scoring = workflowReplay.scoring || {};
  const operationalFacts = workflowReplay.operationalFacts || [];
  const checkpoint = workflowReplay.latestCheckpoint;
  return (
    <section className="workflow-evidence" aria-label={uiText("Bằng chứng workflow", "Workflow evidence") }>
      <div className="section-label">:: {uiText("artifact index", "artifact index")}</div>
      <details className="workflow-details" open>
        <summary>{formatConsoleInt(artifacts.length)} artifact</summary>
        <div className="workflow-artifact-list">
          {artifacts.map(row => {
            const binding = row.binding || {};
            const active = selectedArtifact === binding.artifact_ref;
            return (
              <button
                type="button"
                className={`workflow-artifact-row${active ? " active" : ""}`}
                key={binding.artifact_ref}
                onClick={() => onSelectArtifact && onSelectArtifact(binding.artifact_ref)}
                title={`${binding.artifact_ref}\n${binding.sha256 || ""}`}
              >
                <span>{binding.artifact_kind || "unknown"}</span>
                <code>{binding.artifact_ref || "null"}</code>
                <code>{consoleWorkflowShortHash(binding.sha256)}</code>
              </button>
            );
          })}
        </div>
      </details>

      <div className="section-label">:: {uiText("evaluation bindings", "evaluation bindings")}</div>
      <div className="kv-row"><span className="kv-label">handoff</span><span className="kv-value workflow-mono" title={scoring.handoff?.artifact_ref || ""}>{scoring.handoff ? consoleWorkflowShortHash(scoring.handoff.sha256) : "null"}</span></div>
      <div className="kv-row"><span className="kv-label">receipt</span><span className="kv-value workflow-mono" title={scoring.receipt?.artifact_ref || ""}>{scoring.receipt ? consoleWorkflowShortHash(scoring.receipt.sha256) : "null"}</span></div>
      <div className="kv-row"><span className="kv-label">receipt status</span><span className="kv-value">{scoring.receiptStatus ?? "null"}</span></div>
      <div className="kv-row"><span className="kv-label">report</span><span className="kv-value">{scoring.reports?.length ? scoring.reports.map(report => consoleWorkflowShortHash(report.sha256)).join(", ") : "null"}</span></div>
      <div className="workflow-arm-list" aria-label={uiText("Năm nhánh chấm điểm", "Five scoring arms") }>
        {(scoring.arms || []).map(arm => (
          <div className="workflow-arm-row" key={arm.arm_id}>
            <strong>{arm.arm_id}</strong>
            <span>{arm.producer?.component_run_id || "null"}</span>
            <code>{consoleWorkflowShortHash(arm.translation_artifact?.sha256)}</code>
          </div>
        ))}
      </div>

      <div className="section-label">:: {uiText("checkpoint · usage · cost · cache", "checkpoint · usage · cost · cache")}</div>
      <div className="kv-row"><span className="kv-label">checkpoint</span><span className="kv-value">{checkpoint ? `#${checkpoint.seq} ${checkpoint.payload?.checkpoint ?? checkpoint.event}` : "null"}</span></div>
      {operationalFacts.length ? (
        <details className="workflow-details">
          <summary>{formatConsoleInt(operationalFacts.length)} {uiText("payload đã lưu", "persisted payloads")}</summary>
          <div className="workflow-operational-list">
            {operationalFacts.slice(-6).map(fact => (
              <div className="workflow-operational-row" key={`${fact.seq}:${fact.event}`}>
                <span>#{fact.seq} · {fact.event}</span>
                <pre>{JSON.stringify(fact.payload, null, 2)}</pre>
              </div>
            ))}
          </div>
        </details>
      ) : <div className="artifact-path kv-dim">{uiText("Không có fact usage/cost/cache trong stream; giữ nguyên null.", "No usage/cost/cache facts in the stream; null is preserved.")}</div>}
    </section>
  );
}

function AgentConsoleView(props) {
  const {
    runId: providedRunId, runs = [], onSelectRun,
    events: providedEvents = [], running = false, status = "",
    truncated = false, partialLine = false,
    blockPreview = [], watchlist = [],
    reportSummary = null,
    workflowReplay = null,
    projectId = "", onBack, onOpenReport,
    theme = "paper", onToggleTheme,
    onRefresh, onPause, onCancel, onResume, onDich, busy = false,
  } = props;
  const workflowInvalid = Boolean(workflowReplay && workflowReplay.valid !== true);
  const workflowManifest = workflowReplay?.manifest || null;
  const runId = workflowManifest?.workflow_run_id || providedRunId;
  const events = workflowReplay ? (workflowReplay.valid ? workflowReplay.events || [] : []) : providedEvents;
  const consoleStagePlan = workflowReplay
    ? (workflowReplay.valid && workflowReplay.stagePlan?.length ? workflowReplay.stagePlan : [])
    : CONSOLE_STAGE_PLAN;
  const workflowReadOnly = Boolean(workflowReplay);

  const [stageFilter, setStageFilter] = React.useState("");
  const [agentFilter, setAgentFilter] = React.useState("");
  const [severityFilter, setSeverityFilter] = React.useState("");
  const [heartbeatMode, setHeartbeatMode] = React.useState("grouped");
  const [eventPreset, setEventPreset] = React.useState("important");
  const [uiLocale, setUiLocale] = useThesisLocale();
  const [systemsOpen, setSystemsOpen] = React.useState(false);
  const systemsRef = React.useRef(null);
  const [navigationTarget, setNavigationTarget] = React.useState(null);
  const [navigationNotice, setNavigationNotice] = React.useState(null);
  const [highlightedStage, setHighlightedStage] = React.useState("");
  const [selectedArtifact, setSelectedArtifact] = React.useState("");
  const [selectedEventKey, setSelectedEventKey] = React.useState("");
  const navigationTokenRef = React.useRef(0);
  const [consoleLayout, setConsoleLayout] = React.useState(() => consoleReadLayout());
  const mainColumnRef = React.useRef(null);
  const resizeCleanupRef = React.useRef(null);
  React.useEffect(() => consoleWriteLayout(consoleLayout), [consoleLayout]);
  React.useEffect(() => () => {
    if (resizeCleanupRef.current) resizeCleanupRef.current();
  }, []);

  function setConsoleCenterMode(centerMode) {
    setConsoleLayout(layout => ({ ...layout, centerMode }));
  }

  function updateConsolePreferences(patch) {
    setConsoleLayout(layout => ({ ...layout, ...patch }));
  }

  function resetConsoleLayout() {
    setConsoleLayout({ ...CONSOLE_LAYOUT_DEFAULTS });
  }

  function toggleConsoleSide(side) {
    const key = side === "left" ? "leftCollapsed" : "rightCollapsed";
    setConsoleLayout(layout => ({ ...layout, [key]: !layout[key] }));
  }

  function resetConsoleTrack(track) {
    setConsoleLayout(layout => {
      if (track === "left") return { ...layout, leftWidth: CONSOLE_LAYOUT_DEFAULTS.leftWidth, leftCollapsed: false };
      if (track === "right") return { ...layout, rightWidth: CONSOLE_LAYOUT_DEFAULTS.rightWidth, rightCollapsed: false };
      return { ...layout, ledgerPercent: CONSOLE_LAYOUT_DEFAULTS.ledgerPercent, centerMode: "split" };
    });
  }

  function beginConsoleResize(track, event) {
    if (event.button !== 0) return;
    event.preventDefault();
    if (resizeCleanupRef.current) resizeCleanupRef.current();
    const startX = event.clientX;
    const startY = event.clientY;
    const startLayout = { ...consoleLayout };
    const mainHeight = Math.max(1, mainColumnRef.current?.getBoundingClientRect().height || 1);
    if (track === "left" && startLayout.leftCollapsed) setConsoleLayout(layout => ({ ...layout, leftCollapsed: false }));
    if (track === "right" && startLayout.rightCollapsed) setConsoleLayout(layout => ({ ...layout, rightCollapsed: false }));
    document.body.classList.add("console-resizing", `console-resizing-${track}`);

    const onPointerMove = moveEvent => {
      const deltaX = moveEvent.clientX - startX;
      const deltaY = moveEvent.clientY - startY;
      setConsoleLayout(layout => {
        if (track === "left") {
          return {
            ...layout,
            leftCollapsed: false,
            leftWidth: consoleClamp(startLayout.leftWidth + deltaX, CONSOLE_LAYOUT_LIMITS.leftMin, CONSOLE_LAYOUT_LIMITS.leftMax),
          };
        }
        if (track === "right") {
          return {
            ...layout,
            rightCollapsed: false,
            rightWidth: consoleClamp(startLayout.rightWidth - deltaX, CONSOLE_LAYOUT_LIMITS.rightMin, CONSOLE_LAYOUT_LIMITS.rightMax),
          };
        }
        return {
          ...layout,
          centerMode: "split",
          ledgerPercent: consoleClamp(startLayout.ledgerPercent - (deltaY / mainHeight) * 100, CONSOLE_LAYOUT_LIMITS.ledgerMin, CONSOLE_LAYOUT_LIMITS.ledgerMax),
        };
      });
    };
    const stop = () => {
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerup", stop);
      document.removeEventListener("pointercancel", stop);
      document.body.classList.remove("console-resizing", `console-resizing-${track}`);
      if (resizeCleanupRef.current === stop) resizeCleanupRef.current = null;
    };
    resizeCleanupRef.current = stop;
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", stop);
    document.addEventListener("pointercancel", stop);
  }

  function handleConsoleSplitterKey(track, event) {
    if (!event.key.startsWith("Arrow")) return;
    event.preventDefault();
    setConsoleLayout(layout => {
      if (track === "left" && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
        const delta = event.key === "ArrowRight" ? 12 : -12;
        return { ...layout, leftCollapsed: false, leftWidth: consoleClamp(layout.leftWidth + delta, CONSOLE_LAYOUT_LIMITS.leftMin, CONSOLE_LAYOUT_LIMITS.leftMax) };
      }
      if (track === "right" && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
        const delta = event.key === "ArrowLeft" ? 12 : -12;
        return { ...layout, rightCollapsed: false, rightWidth: consoleClamp(layout.rightWidth + delta, CONSOLE_LAYOUT_LIMITS.rightMin, CONSOLE_LAYOUT_LIMITS.rightMax) };
      }
      if (track === "ledger" && ["ArrowUp", "ArrowDown"].includes(event.key)) {
        const delta = event.key === "ArrowUp" ? 4 : -4;
        return { ...layout, centerMode: "split", ledgerPercent: consoleClamp(layout.ledgerPercent + delta, CONSOLE_LAYOUT_LIMITS.ledgerMin, CONSOLE_LAYOUT_LIMITS.ledgerMax) };
      }
      return layout;
    });
  }
  // Client-side replay only: seeking recomputes the visible state from saved events.
  // It never calls the backend or mutates the persisted run.
  const [replayCursor, setReplayCursor] = React.useState(null);
  const [replayPlaying, setReplayPlaying] = React.useState(false);
  const [replaySpeed, setReplaySpeed] = React.useState(1);
  const [replayMode, setReplayMode] = React.useState("time");
  const replayTimer = React.useRef(null);
  const stopReplayTimer = React.useCallback(() => {
    if (replayTimer.current) { clearTimeout(replayTimer.current); replayTimer.current = null; }
  }, []);
  React.useEffect(() => () => stopReplayTimer(), [stopReplayTimer]);
  React.useEffect(() => {
    setReplayCursor(null);
    setReplayPlaying(false);
    setSystemsOpen(false);
    setNavigationTarget(null);
    setNavigationNotice(null);
    setHighlightedStage("");
    setSelectedArtifact("");
    setSelectedEventKey("");
    stopReplayTimer();
  }, [runId, stopReplayTimer]);
  React.useEffect(() => {
    if (!systemsOpen) return undefined;
    function closeOnPointerDown(event) {
      if (systemsRef.current && !systemsRef.current.contains(event.target)) setSystemsOpen(false);
    }
    function closeOnEscape(event) {
      if (event.key === "Escape") setSystemsOpen(false);
    }
    document.addEventListener("pointerdown", closeOnPointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnPointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [systemsOpen]);
  React.useEffect(() => {
    stopReplayTimer();
    if (!replayPlaying || replayCursor == null) return undefined;
    if (replayCursor >= events.length) {
      setReplayPlaying(false);
      return undefined;
    }
    replayTimer.current = setTimeout(() => {
      setReplayCursor(current => Math.min(events.length, Number(current || 0) + 1));
    }, consoleReplayDelay(events, replayCursor, replayMode, replaySpeed));
    return stopReplayTimer;
  }, [events, replayCursor, replayPlaying, replayMode, replaySpeed, stopReplayTimer]);

  function toggleReplay() {
    if (!events.length) return;
    if (replayPlaying) {
      setReplayPlaying(false);
      return;
    }
    if (replayCursor == null || replayCursor >= events.length) setReplayCursor(0);
    setReplayPlaying(true);
  }

  function seekReplay(value) {
    const next = Math.max(0, Math.min(events.length, Number(value)));
    setReplayCursor(next);
    if (next >= events.length) setReplayPlaying(false);
  }

  function restartReplay() {
    setReplayCursor(0);
    setReplayPlaying(true);
  }

  function finishReplay() {
    setReplayCursor(events.length);
    setReplayPlaying(false);
  }

  const replayActive = replayCursor != null;
  const replayPosition = replayActive ? replayCursor : events.length;
  const shownEvents = replayActive ? events.slice(0, replayPosition) : events;
  const fullState = React.useMemo(() => deriveConsoleState(events, consoleStagePlan, true), [events, consoleStagePlan]);

  const st = React.useMemo(() => deriveConsoleState(shownEvents, consoleStagePlan, !replayActive), [shownEvents, consoleStagePlan, replayActive]);
  const sourceRunStatus = workflowInvalid ? "failed" : (fullState.runStatus || workflowManifest?.status || status || (running ? "running" : "idle"));
  const sourceIsTerminal = !!runId && consoleIsTerminalStatus(sourceRunStatus);
  const workflowRecorded = workflowReplay?.sourceMode === "replay";
  const replayAvailable = events.length > 0 && (sourceIsTerminal || workflowRecorded);
  const replayComplete = replayActive && replayPosition >= events.length;
  const runStatus = replayActive
    ? (st.runStatus || (replayComplete ? sourceRunStatus : "running"))
    : sourceRunStatus;
  const hasRun = !!runId;
  const isTerminal = hasRun && consoleIsTerminalStatus(runStatus);
  const isOpenRun = hasRun && !sourceIsTerminal && !workflowRecorded && !workflowInvalid;
  const stalled = !replayActive && isOpenRun && running && consoleAgeSeconds(st.lastTs) > 90;
  const consoleMode = workflowInvalid ? "INVALID" : replayActive ? "REPLAY" : workflowRecorded ? "RECORDED" : isOpenRun ? "LIVE" : hasRun ? "RECORDED" : "IDLE";
  const playbackState = replayActive
    ? (replayComplete
      ? !sourceIsTerminal ? "SNAPSHOT"
        : sourceRunStatus === "failed" ? "FAILED"
        : sourceRunStatus === "cancelled" ? "CANCELLED"
        : "DONE"
      : replayPlaying ? "PLAYING" : "PAUSED")
    : workflowInvalid ? "INVALID"
      : workflowRecorded && !sourceIsTerminal ? "SNAPSHOT"
      : stalled ? "STALLED"
      : st.paused ? "PAUSED"
        : isOpenRun && !running ? "CONNECTING"
          : String(runStatus || "idle").toUpperCase();
  const canResumeRun = !replayActive && !!onResume && (runStatus === "failed" || st.paused || stalled);
  const replayClock = consoleReplayClock(events, replayPosition);
  const systemChecks = st.systemChecks || [];
  const readySystems = systemChecks.filter(check => check.ok === true).length;
  const failedSystems = systemChecks.filter(check => check.ok === false).length;
  const unknownSystems = systemChecks.length - readySystems - failedSystems;
  const preflightDone = st.stageInfo.preflight_check?.status === "done";
  const systemsTone = failedSystems ? "bad" : unknownSystems || (systemChecks.length && !preflightDone) ? "warn" : systemChecks.length ? "good" : "dim";
  const systemsCount = systemChecks.length
    ? (preflightDone ? `${readySystems}/${systemChecks.length}` : `${readySystems} ready`)
    : "—";

  const displayRows = React.useMemo(() => consoleHeartbeatRows(st.normalized, heartbeatMode), [st.normalized, heartbeatMode]);
  const agents = uniqueConsole(st.normalized.map(r => r.agent).filter(Boolean));
  const severities = uniqueConsole(st.normalized.map(r => r.severity).filter(Boolean));
  const presetRows = displayRows.filter(r => eventPreset === "all" || consoleIsImportantEvent(r));
  const filtered = presetRows.filter(r =>
    (!stageFilter || r.stage === stageFilter)
    && (!agentFilter || r.agent === agentFilter)
    && (!severityFilter || r.severity === severityFilter));
  const filteredRawEventCount = filtered.reduce((sum, row) => sum + Number(row.rawEventCount || 1), 0);
  const hiddenOlderEvents = Math.max(0, filtered.length - CONSOLE_RENDER_CAP);
  const rendered = filtered.slice(-CONSOLE_RENDER_CAP).reverse();
  const costPct = st.budgetCap ? Math.min(100, Math.round((st.cumulativeCost / st.budgetCap) * 100)) : 0;
  const healthClass = ["FAILED", "STALLED", "INVALID"].includes(playbackState) ? "kv-bad"
    : ["PLAYING", "PAUSED", "CONNECTING"].includes(playbackState) ? "kv-warn"
      : ["RUNNING", "DONE", "SNAPSHOT"].includes(playbackState) ? "kv-good" : "kv-dim";
  const statusChipClass = ["FAILED", "STALLED", "INVALID"].includes(playbackState) ? "hdr-status-bad"
    : ["PLAYING", "PAUSED", "CONNECTING"].includes(playbackState) ? "hdr-status-warn"
      : ["RUNNING", "DONE", "SNAPSHOT"].includes(playbackState) ? "hdr-status-good" : "";
  const reportReached = !workflowReplay && (!replayActive
    || st.stageInfo.score_run_phase_1.status === "done"
    || st.stageInfo.score_run_final.status === "done"
    || !!st.runStatus);
  const visibleReportSummary = reportReached ? reportSummary : null;
  const visibleWatchlist = workflowReplay ? [] : (!replayActive || st.stageInfo.reelection_watchlist?.status === "done" ? watchlist : []);
  const reportCfgs = (visibleReportSummary && visibleReportSummary.phase_1 && visibleReportSummary.phase_1.configs) || [];
  const isCompareRun = reportCfgs.includes("S0") || !!(visibleReportSummary && visibleReportSummary.compare && visibleReportSummary.compare.present);
  const workflowArms = workflowReplay?.valid ? (workflowReplay.scoring?.arms || []).map(arm => arm.arm_id) : [];
  const armsLabel = workflowArms.length ? `${workflowArms.length} arms` : reportCfgs.length ? (isCompareRun ? "S0+S1" : reportCfgs.join("+")) : (isCompareRun ? "S0+S1" : null);
  const compareGap = visibleReportSummary && visibleReportSummary.compare && visibleReportSummary.compare.present ? (visibleReportSummary.compare.gap || {}) : null;
  const finalGate = visibleReportSummary && visibleReportSummary.final && visibleReportSummary.final.stage_gate && visibleReportSummary.final.stage_gate.present ? visibleReportSummary.final.stage_gate : null;
  const finalGateText = formatConsoleGate(finalGate);
  const consistencySummary = visibleReportSummary && visibleReportSummary.consistency && visibleReportSummary.consistency.present ? visibleReportSummary.consistency : null;
  const consistencyTierRows = consoleConsistencyTierRows(consistencySummary);

  // Stored rows provide text and final per-arm totals. Replay-visible events
  // alone advance each arm, so S1 cannot appear while only S0 has run.
  const previewProgress = st.previewWindowsByArm || {};
  const previewTotals = consolePreviewArmTotals(blockPreview);
  Object.entries(st.translatorTotalsByArm || {}).forEach(([arm, total]) => {
    previewTotals[arm] = Math.max(Number(previewTotals[arm] || 0), Number(total || 0));
  });
  const previewAvailable = Object.values(previewProgress).some(value => Number(value) > 0);
  const previews = consolePreviewRowsThroughProgress(blockPreview, previewProgress);
  const translatorProgressLabel = consolePreviewProgressLabel(previewProgress, previewTotals);
  const currentStageId = consoleCurrentStageId(st.normalized, consoleStagePlan) || workflowManifest?.active_stage_id || "";
  const currentStageMeta = consoleStagePlan.find(stage => stage.id === currentStageId);
  const currentStageLabel = currentStageMeta?.label || consoleText(uiLocale, "noneYet");
  const cursorTextKey = consoleMode === "REPLAY"
    ? "replayCursorAt"
    : consoleMode === "LIVE" ? "liveCursorAt" : "recordedCursorAt";

  function nextNavigationTarget(kind, id) {
    navigationTokenRef.current += 1;
    const target = { kind, id: String(id || ""), token: navigationTokenRef.current };
    setNavigationTarget(target);
    return target;
  }

  function handleNavigationResult(result) {
    if (!result?.ok) {
      setNavigationNotice({ tone: "warn", text: consoleText(uiLocale, "navigationTargetUnavailable") });
      return;
    }
    const textKey = result.kind === "memory" ? "navigationMemoryReady" : "navigationBlockReady";
    setNavigationNotice({ tone: "ok", text: consoleText(uiLocale, textKey) });
  }

  function navigateToStage(stageId) {
    const available = st.normalized.some(row => row.stage === stageId);
    if (!available) {
      setNavigationNotice({ tone: "warn", text: consoleText(uiLocale, "navigationTargetUnavailable") });
      return;
    }
    setStageFilter(stageId);
    setHighlightedStage(stageId);
    setSelectedEventKey("");
    nextNavigationTarget("stage", stageId);
    setNavigationNotice({ tone: "ok", text: `${consoleText(uiLocale, "navigationStageFilter")}: ${stageId}` });
  }

  function navigateFromEvent(row) {
    const hasTarget = Boolean(row.memoryDeltaId || row.blockId || row.artifactPath || row.stage);
    if (!hasTarget) return;
    setSelectedEventKey(row.key);
    if (row.stage) setHighlightedStage(row.stage);
    if (row.memoryDeltaId) {
      setSelectedArtifact("");
      nextNavigationTarget("memory", row.memoryDeltaId);
      return;
    }
    if (row.blockId) {
      setSelectedArtifact("");
      nextNavigationTarget("block", row.blockId);
      return;
    }
    if (row.artifactPath) {
      setSelectedArtifact(row.artifactPath);
      nextNavigationTarget("artifact", row.artifactPath);
      setNavigationNotice({ tone: "ok", text: consoleText(uiLocale, "navigationArtifactReady") });
      return;
    }
    navigateToStage(row.stage);
  }

  function clearConsoleNavigation() {
    setNavigationTarget(null);
    setNavigationNotice(null);
    setHighlightedStage("");
    setSelectedArtifact("");
    setSelectedEventKey("");
    setStageFilter("");
  }

  React.useEffect(() => {
    if (!navigationTarget) return;
    let available = false;
    if (navigationTarget.kind === "block") {
      available = previews.some(row => String(row.block_id || "") === navigationTarget.id);
    } else if (navigationTarget.kind === "memory") {
      available = st.memoryDeltas.some(delta => delta.deltaId === navigationTarget.id);
    } else if (navigationTarget.kind === "artifact") {
      available = st.normalized.some(row => row.artifactPath === navigationTarget.id);
    } else if (navigationTarget.kind === "stage") {
      available = st.normalized.some(row => row.stage === navigationTarget.id);
    }
    if (available) return;
    setNavigationTarget(null);
    setSelectedEventKey("");
    setSelectedArtifact("");
    setHighlightedStage("");
    setNavigationNotice({ tone: "warn", text: consoleText(uiLocale, "navigationTargetUnavailable") });
  }, [navigationTarget, previews, st.memoryDeltas, st.normalized, uiLocale]);

  return (
    <div className={`agentconsole console-theme-${theme}${workflowReplay ? " workflow-console" : ""}`}>
      <header className="console-header">
        {onBack && <button className="btn console-back" type="button" onClick={onBack}>&larr; {consoleText(uiLocale, "workspace")}</button>}
        <span className="brand">⬢ AGENT CONSOLE</span>
        <nav className="run-surface-tabs" aria-label={uiText("Các chế độ lần chạy", "Run views")}>
          <span className="run-surface-tab active" aria-current="page">Console</span>
          {onOpenReport && (
            <button className="run-surface-tab" type="button" onClick={onOpenReport}>{uiText("Báo cáo", "Report")}</button>
          )}
        </nav>
        {projectId && <span className="console-project" title={projectId}>{projectId}</span>}
        <select className="run-picker" aria-label={uiText("Chọn lần chạy", "Run picker")} value={runId || ""} onChange={e => onSelectRun && onSelectRun(e.target.value)}>
          {!runId && <option value="">{consoleText(uiLocale, "selectRun")}</option>}
          {runs.slice(0, 40).map(r => {
            const t = r.started_at ? new Date(r.started_at) : null;
            const stamp = t && !isNaN(t.getTime())
              ? " · " + String(t.getMonth()+1).padStart(2,"0") + "-" + String(t.getDate()).padStart(2,"0")
                + " " + String(t.getHours()).padStart(2,"0") + ":" + String(t.getMinutes()).padStart(2,"0")
              : "";
            return <option key={r.run_id} value={r.run_id}>{r.run_id}{r.status ? " · " + r.status : ""}{stamp}</option>;
          })}
        </select>
        <span className="systems-health" ref={systemsRef}>
          <button
            className={`systems-trigger systems-${systemsTone}`}
            type="button"
            aria-expanded={systemsOpen}
            aria-haspopup="dialog"
            onClick={() => setSystemsOpen(open => !open)}
            title={uiText("Trạng thái model, API và dependency từ preflight của lần chạy đang chọn", "Model, API, and dependency status from the selected run preflight")}
          >
            <span className="systems-dot" aria-hidden="true">●</span>
            <span>{uiText("HỆ THỐNG", "SYSTEMS")}</span>
            <span className="systems-count">{systemsCount}</span>
          </button>
          {systemsOpen && (
            <span className="systems-popover" role="dialog" aria-label={uiText("Trạng thái preflight của model và API", "Model and API preflight status")}>
              <span className="systems-popover-head">
                <span>{uiText("TRẠNG THÁI MODEL & API", "MODEL & API STATUS")}</span>
                <span className="systems-popover-summary">{systemChecks.length ? uiText(`${readySystems} sẵn sàng${failedSystems ? ` · ${failedSystems} lỗi` : ""}`, `${readySystems} ready${failedSystems ? ` · ${failedSystems} failed` : ""}`) : uiText("chưa kiểm tra", "not checked")}</span>
              </span>
              {systemChecks.length ? systemChecks.map(check => (
                <span className="systems-row" key={check.id}>
                  <span className={`systems-row-dot systems-${check.ok === true ? "good" : check.ok === false ? "bad" : "warn"}`} aria-hidden="true">●</span>
                  <span className="systems-row-copy">
                    <span className="systems-row-title">{consoleSystemLabel(check.id)}</span>
                    <span className="systems-row-detail">{consoleSystemDetail(check)}</span>
                  </span>
                  <span className={`systems-row-state systems-${check.ok === true ? "good" : check.ok === false ? "bad" : "warn"}`}>
                    {consoleSystemStateLabel(check)}
                    {consoleSystemTime(check.ts) && <span className="systems-row-time">{consoleSystemTime(check.ts)}</span>}
                  </span>
                </span>
              )) : (
                <span className="systems-empty">{uiText("Chưa có sự kiện health_check ở vị trí replay hiện tại.", "No health_check event is available at the current replay position.")}</span>
              )}
              <span className="systems-note">{uiText("Snapshot preflight 0-API · kiểm tra key không xác nhận quota/provider", "0-API preflight snapshot · key checks do not confirm quota/provider")}</span>
            </span>
          )}
        </span>
        <span className="hdr-actions">
          {!workflowReadOnly && onDich && <button className="btn btn-accent" type="button" disabled={busy || isOpenRun} onClick={onDich} title={consoleText(uiLocale, "translate")}>▸ {consoleText(uiLocale, "translate").toUpperCase()}</button>}
          {replayAvailable && <button className="btn" type="button" onClick={toggleReplay} title={consoleText(uiLocale, replayPlaying ? "pause" : replayActive && !replayComplete ? "play" : "replay")}>{consoleText(uiLocale, replayPlaying ? "pause" : replayActive && !replayComplete ? "play" : "replay")}</button>}
          <button className="btn" type="button" disabled={busy} onClick={onRefresh}>↻ {consoleText(uiLocale, "refresh")}</button>
          {!workflowReadOnly && <button className="btn" type="button" disabled={!isOpenRun || !onPause} onClick={onPause}>⏸ {consoleText(uiLocale, "pauseAfterStage")}</button>}
          {!workflowReadOnly && (canResumeRun
            ? <button className="btn btn-accent" type="button" onClick={onResume}>▸ {consoleText(uiLocale, "resume")}</button>
            : <button className="btn btn-danger" type="button" disabled={!isOpenRun || !onCancel} onClick={onCancel}>✕ {consoleText(uiLocale, "cancel")}</button>)}
          <ThesisLocaleSwitch compact locale={uiLocale} onChange={setUiLocale} />
          <button
            className="btn btn-icon"
            type="button"
            onClick={resetConsoleLayout}
            title={consoleText(uiLocale, "resetLayout")}
            aria-label={consoleText(uiLocale, "resetLayout")}
          >
            ↺
          </button>
          <button className="btn" type="button" onClick={onToggleTheme}>◐ {consoleText(uiLocale, "theme")}</button>
          <span
            className="hdr-status hdr-status-mode"
            title={`${consoleText(uiLocale, "mode")}: ${consoleMode}`}
          >
            {consoleMode}
          </span>
          <span
            className={"hdr-status " + statusChipClass}
            title={`${consoleText(uiLocale, "state")}: ${playbackState}`}
          >
            {playbackState}
          </span>
          {armsLabel && <span className={"hdr-status " + (workflowArms.length || isCompareRun ? "hdr-status-good" : "")} title={workflowArms.length ? workflowArms.join(", ") : isCompareRun ? uiText("Chạy cả S0 và S1 (có so sánh)", "Runs both S0 and S1 (comparison)") : uiText("Chỉ S1", "S1 only")}>{armsLabel}</span>}
        </span>
      </header>

      {replayAvailable && (
        <div className="replay-controls" aria-label={uiText("Điều khiển timeline phát lại", "Replay timeline controls")}>
          <button className="btn btn-icon" type="button" onClick={restartReplay} title={uiText("Phát lại từ đầu", "Restart replay")} aria-label={uiText("Phát lại từ đầu", "Restart replay")}>|&lt;</button>
          <button className="btn btn-icon btn-accent" type="button" onClick={toggleReplay} title={replayPlaying ? uiText("Tạm dừng phát lại", "Pause replay") : uiText("Phát", "Play replay")} aria-label={replayPlaying ? uiText("Tạm dừng phát lại", "Pause replay") : uiText("Phát", "Play replay")}>{replayPlaying ? "||" : ">"}</button>
          <input
            className="replay-range"
            type="range"
            min="0"
            max={events.length}
            step="1"
            value={replayPosition}
            onChange={event => seekReplay(event.target.value)}
            aria-label={uiText("Vị trí phát lại", "Replay position")}
          />
          <span className="replay-count">{formatConsoleInt(replayPosition)} / {formatConsoleInt(events.length)}</span>
          <span className="replay-clock">{replayClock.elapsed} / {replayClock.total}</span>
          <select className="filter-select replay-mode" value={replayMode} onChange={event => setReplayMode(event.target.value)} aria-label={uiText("Chế độ thời gian phát lại", "Replay timing mode")}>
            <option value="time">timestamp</option>
            <option value="event">{uiText("sự kiện", "events")}</option>
          </select>
          <select className="filter-select replay-speed" value={replaySpeed} onChange={event => setReplaySpeed(Number(event.target.value))} aria-label={uiText("Tốc độ phát lại", "Replay speed")}>
            {CONSOLE_REPLAY_SPEEDS.map(speed => <option key={speed} value={speed}>{speed}x</option>)}
          </select>
          <button className="btn btn-icon" type="button" onClick={finishReplay} title={uiText("Nhảy tới cuối", "Jump to end")} aria-label={uiText("Nhảy tới cuối", "Jump to end")}>&gt;|</button>
        </div>
      )}

      <div
        className="console-body"
        style={{
          "--console-left-width": `${consoleLayout.leftCollapsed ? 34 : consoleLayout.leftWidth}px`,
          "--console-right-width": `${consoleLayout.rightCollapsed ? 34 : consoleLayout.rightWidth}px`,
        }}
      >
        {/* ---------------- LEFT ---------------- */}
        <aside className={"col col-left" + (consoleLayout.leftCollapsed ? " console-side-collapsed" : "")}>
          <button
            type="button"
            className="console-side-toggle console-side-toggle-left"
            onClick={() => toggleConsoleSide("left")}
            title={consoleLayout.leftCollapsed ? uiText("Mở Tổng quan", "Expand Overview") : uiText("Thu gọn Tổng quan", "Collapse Overview")}
            aria-label={consoleLayout.leftCollapsed ? uiText("Mở panel Tổng quan", "Expand Overview panel") : uiText("Thu gọn panel Tổng quan", "Collapse Overview panel")}
            aria-expanded={!consoleLayout.leftCollapsed}
          >
            {consoleLayout.leftCollapsed ? "›" : "‹"}
          </button>
          <div className="console-side-content">
          <ConsoleWorkflowIdentity workflowReplay={workflowReplay} uiText={uiText} />
          <div className="section-label">:: {uiText("tổng quan", "overview")}</div>
          <div className="kv-row"><span className="kv-label">{consoleText(uiLocale, "mode")}</span><span className="kv-value kv-dim">{consoleMode}</span></div>
          <div className="kv-row"><span className="kv-label">{consoleText(uiLocale, "state")}</span><span className={"kv-value " + healthClass}>{playbackState}</span></div>
          {armsLabel && <div className="kv-row"><span className="kv-label">{uiText("nhánh", "arms")}</span><span className={"kv-value " + (isCompareRun ? "kv-good" : "kv-dim")}>{armsLabel}</span></div>}
          <div className="kv-row"><span className="kv-label">{uiText("tầng đã thấy", "stages seen")}</span><span className="kv-value">{st.stagesSeen} / {consoleStagePlan.length}</span></div>
          <div className="kv-row"><span className="kv-label">{uiText("sự kiện", "events")}</span><span className="kv-value">{formatConsoleInt(st.totalEvents)}</span></div>
          <div className="kv-row"><span className="kv-label">{uiText("luồng", "stream")}</span><span className="kv-value kv-dim">{truncated ? uiText("bị cắt", "truncated") : partialLine ? uiText("dòng chưa đủ", "partial line") : isOpenRun ? (running ? uiText("trực tiếp", "live") : uiText("đang kết nối", "connecting")) : uiText("đã đóng", "closed")}</span></div>

          <div className="section-label">:: {uiText("chi phí & cache", "cost & cache")}</div>
          {workflowReplay ? (
            <div className="artifact-path kv-dim">{uiText("Không cộng lại ở UI; xem payload đã lưu ở panel phải. Giá trị thiếu giữ nguyên null.", "No UI aggregation; see persisted payloads in the right panel. Missing values remain null.")}</div>
          ) : (
            <>
              <div className="kv-row"><span className="kv-label">{uiText("tổng cap", "cap total")}</span><span className="kv-value">{st.cumulativeCost != null ? "$" + st.cumulativeCost.toFixed(4) : "—"}</span></div>
              <div className="kv-row kv-row-bar"><span className="kv-label">{uiText("cap / ngân sách", "cap / budget")}</span><span className="kv-value kv-dim">{st.cumulativeCost != null ? "$" + st.cumulativeCost.toFixed(3) : "—"} / {st.budgetCap != null ? "$" + st.budgetCap.toFixed(2) : "—"}</span></div>
              <div className="bar"><div className="bar-fill" style={{ width: costPct + "%" }} /></div>
              <div className="kv-row"><span className="kv-label">{uiText("sự kiện LLM", "LLM events")}</span><span className="kv-value">{formatConsoleInt(st.llmCalls)}</span></div>
            </>
          )}

          <div className="section-label">:: {uiText("sức khỏe", "health")}</div>
          <div className="kv-row"><span className="kv-label">{uiText("cảnh báo", "warnings")}</span><span className={"kv-value " + (st.warnings ? "kv-warn" : "")}>{formatConsoleInt(st.warnings)}</span></div>
          <div className="kv-row"><span className="kv-label">{uiText("lỗi", "errors")}</span><span className={"kv-value " + (st.errors ? "kv-bad" : "")}>{formatConsoleInt(st.errors)}</span></div>
          <div className="kv-row"><span className="kv-label">{uiText("sự kiện cuối", "last event")}</span><span className="kv-value kv-dim">{st.lastTs ? st.lastTs.slice(11, 19) : "—"}</span></div>
          </div>
        </aside>

        <div
          className="console-splitter console-splitter-vertical"
          role="separator"
          aria-label={uiText("Đổi kích thước panel Tổng quan", "Resize Overview panel")}
          aria-orientation="vertical"
          aria-valuemin={0}
          aria-valuemax={CONSOLE_LAYOUT_LIMITS.leftMax}
          aria-valuenow={consoleLayout.leftCollapsed ? 0 : Math.round(consoleLayout.leftWidth)}
          tabIndex="0"
          title={uiText("Kéo để đổi chiều rộng Tổng quan; nhấp đúp để đặt lại", "Drag to resize Overview; double-click to reset")}
          onPointerDown={event => beginConsoleResize("left", event)}
          onKeyDown={event => handleConsoleSplitterKey("left", event)}
          onDoubleClick={() => resetConsoleTrack("left")}
        />

        {/* ---------------- MAIN ---------------- */}
        <main
          ref={mainColumnRef}
          className={`col col-main console-main-${consoleLayout.centerMode}`}
          style={{ "--console-ledger-height": `${consoleLayout.ledgerPercent}%` }}
        >
          {workflowInvalid && (
            <div className="banner banner-red workflow-invalid-banner" role="alert">
              <span className="banner-glyph">✕</span>
              <span className="banner-msg">
                <strong>{uiText("Replay bị khóa an toàn", "Replay failed closed")}</strong>
                <span>{uiText("Manifest, chuỗi sự kiện hoặc artifact binding không hợp lệ. Console không hiển thị fact một phần.", "The manifest, event sequence, or artifact binding is invalid. Console will not render partial facts.")}</span>
                <code>{(workflowReplay.errors || []).slice(0, 4).map(error => `${error.code} ${error.path}`).join(" · ") || "workflow_contract_invalid"}</code>
              </span>
            </div>
          )}
          {!workflowInvalid && runStatus === "failed" && (
            <div className="banner banner-red">
              <span className="banner-glyph">✕</span>
              <span className="banner-msg">{uiText("Lần chạy thất bại", "Run failed")}{st.stderrTail.length ? " · " + consoleShort(st.stderrTail[st.stderrTail.length - 1], 70) : ""}</span>
              {!workflowReadOnly && !replayActive && onResume && <span className="banner-actions"><button className="btn btn-mini" onClick={onResume}>{uiText("tiếp tục", "resume")}</button></span>}
            </div>
          )}
          {st.paused && !isTerminal && runStatus !== "failed" && (
            <div className="banner banner-amber">
              <span className="banner-glyph">⏸</span>
              <span className="banner-msg">{uiText("Đã dừng", "Paused")} · {st.pausedReason} — {uiText("tiếp tục để chạy tiếp", "resume to continue")}</span>
              {!workflowReadOnly && !replayActive && onResume && <span className="banner-actions"><button className="btn btn-mini" onClick={onResume}>{uiText("tiếp tục", "resume")}</button></span>}
            </div>
          )}
          {st.phase1Done && runStatus !== "failed" && (
            <div className="banner banner-green">
              <span className="banner-glyph">●</span>
              <span className="banner-msg">
                {consoleText(uiLocale, "phaseReady")} · {consoleText(uiLocale, cursorTextKey)}: {currentStageLabel}
              </span>
            </div>
          )}
          {stalled && (
            <div className="banner banner-red">
              <span className="banner-glyph badge-stalled">▲</span>
              <span className="banner-msg">{uiText(`Không có sự kiện ${Math.round(consoleAgeSeconds(st.lastTs))}s — có thể treo`, `No event for ${Math.round(consoleAgeSeconds(st.lastTs))}s — possibly stalled`)}</span>
              {onResume && <span className="banner-actions"><button className="btn btn-mini" onClick={onResume}>{uiText("tiếp tục", "resume")}</button></span>}
            </div>
          )}

          <section className="console-event-pane" aria-label={uiText("Luồng sự kiện lần chạy", "Run event stream")}>
            <div className="section-label">:: {uiText("luồng sự kiện", "event stream")}</div>
            <div className="filterbar">
              <span className="event-preset" role="group" aria-label={consoleText(uiLocale, "eventPreset")}>
                {["important", "all"].map(preset => (
                  <button
                    type="button"
                    className={"event-preset-option" + (eventPreset === preset ? " active" : "")}
                    aria-pressed={eventPreset === preset}
                    onClick={() => setEventPreset(preset)}
                    key={preset}
                  >
                    {consoleText(uiLocale, preset === "important" ? "importantEvents" : "allEvents")}
                  </button>
                ))}
              </span>
              <select className="filter-select" value={stageFilter} onChange={e => setStageFilter(e.target.value)}>
                <option value="">{uiText("tầng: tất cả", "stage: all")}</option>
                {consoleStagePlan.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}
              </select>
              <select className="filter-select" value={agentFilter} onChange={e => setAgentFilter(e.target.value)}>
                <option value="">{uiText("agent: tất cả", "agent: all")}</option>
                {agents.map(a => <option key={a} value={a}>{a}</option>)}
              </select>
              <select className="filter-select" value={severityFilter} onChange={e => setSeverityFilter(e.target.value)}>
                <option value="">{uiText("mức độ: tất cả", "severity: all")}</option>
                {severities.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
              <select className="filter-select" aria-label={uiText("Hiển thị heartbeat", "Heartbeat display")} value={heartbeatMode} onChange={e => setHeartbeatMode(e.target.value)}>
                <option value="grouped">heartbeat: {uiText("gộp", "grouped")}</option>
                <option value="hidden">heartbeat: {uiText("ẩn", "hidden")}</option>
                <option value="raw">heartbeat: {uiText("chi tiết", "raw")}</option>
              </select>
              <span className="filter-count">{formatConsoleInt(rendered.length)} / {formatConsoleInt(filtered.length)} {uiText("dòng", "rows")} · {formatConsoleInt(filteredRawEventCount)} {uiText("sự kiện", "events")}</span>
              <button
                type="button"
                className="console-pane-mode-button"
                onClick={() => setConsoleCenterMode(consoleLayout.centerMode === "events" ? "split" : "events")}
                title={consoleLayout.centerMode === "events" ? uiText("Khôi phục Event Stream và Run Ledger", "Restore Event Stream and Run Ledger") : uiText("Phóng to Event Stream", "Maximize Event Stream")}
                aria-label={consoleLayout.centerMode === "events" ? uiText("Khôi phục Console chia đôi", "Restore split Console view") : uiText("Phóng to Event Stream", "Maximize Event Stream")}
              >
                {consoleLayout.centerMode === "events" ? "↕" : "□"}
              </button>
            </div>

            {navigationNotice && (
              <div className={`console-navigation-notice notice-${navigationNotice.tone || "ok"}`} role="status">
                <span>{navigationNotice.text}</span>
                <button type="button" onClick={clearConsoleNavigation}>{consoleText(uiLocale, "clearNavigation")}</button>
              </div>
            )}

            <div className="event-feed">
              {rendered.length ? rendered.map(r => {
                const navigable = Boolean(r.memoryDeltaId || r.blockId || r.artifactPath || r.stage);
                return (
                  <div
                    key={r.key}
                    className={"ev-row ev-" + r.severity
                      + (r.isCost ? " ev-cost" : "")
                      + (r.isContext ? " ev-context" : "")
                      + (navigable ? " ev-navigable" : "")
                      + (selectedEventKey === r.key ? " ev-navigation-selected" : "")}
                    role={navigable ? "button" : undefined}
                    tabIndex={navigable ? 0 : undefined}
                    onClick={navigable ? () => navigateFromEvent(r) : undefined}
                    onKeyDown={navigable ? event => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        navigateFromEvent(r);
                      }
                    } : undefined}
                  >
                    <span className="ev-time">{r.ts ? r.ts.slice(11, 19) : "--:--:--"}</span>
                    <span className="ev-glyph">{r.glyph}</span>
                    <span className="ev-body">
                      <b className="ev-type">{r.event}{r.heartbeatCount > 1 ? ` ×${r.heartbeatCount}` : ""}</b>
                      <span className="ev-src">{r.stage || "-"}{r.agent ? " · " + r.agent : ""}</span>
                      <span className="ev-msg">{r.message}</span>
                      {r.dur != null && <span className="ev-dur">({r.dur}s)</span>}
                    </span>
                    <span className="ev-seq">{consoleEventSequenceLabel(r)}</span>
                  </div>
                );
              }) : <div className="console-empty">{workflowInvalid
                ? uiText("Không hiển thị sự kiện vì replay không vượt qua kiểm tra contract.", "Events are hidden because replay contract validation failed.")
                : uiText("Chọn hoặc phát lại một lần chạy để xem dòng sự kiện.", "Select or replay a run to view its event stream.")}</div>}
              {hiddenOlderEvents > 0 && (
                <div className="console-empty">... {formatConsoleInt(hiddenOlderEvents)} {uiText("dòng cũ hơn bị ẩn - dùng bộ lọc để thu hẹp.", "older rows hidden - use filters to narrow the stream.")}</div>
              )}
            </div>
          </section>

          <div
            className="console-splitter console-splitter-horizontal"
            role="separator"
            aria-label={uiText("Đổi kích thước Event Stream và Run Ledger", "Resize Event Stream and Run Ledger")}
            aria-orientation="horizontal"
            aria-valuemin={CONSOLE_LAYOUT_LIMITS.ledgerMin}
            aria-valuemax={CONSOLE_LAYOUT_LIMITS.ledgerMax}
            aria-valuenow={Math.round(consoleLayout.ledgerPercent)}
            tabIndex="0"
            title={uiText("Kéo để đổi chiều cao Run Ledger; nhấp đúp để đặt lại", "Drag to resize Run Ledger; double-click to reset")}
            onPointerDown={event => beginConsoleResize("ledger", event)}
            onKeyDown={event => handleConsoleSplitterKey("ledger", event)}
            onDoubleClick={() => resetConsoleTrack("ledger")}
          />

          <ConsoleMemoryLedger
            deltas={st.memoryDeltas}
            invalidCount={st.invalidMemoryDeltaCount}
            watchlist={visibleWatchlist}
            packSummary={st.latestPackSummary}
            packWindow={st.latestPackWindow}
            consistencyTierRows={consistencyTierRows}
            runKey={runId || ""}
            previews={previews}
            previewAvailable={previewAvailable}
            previewProgress={previewProgress}
            previewTotals={previewTotals}
            latestPreviewArm={st.latestPreviewArm}
            centerMode={consoleLayout.centerMode}
            onCenterMode={setConsoleCenterMode}
            layoutPreferences={consoleLayout}
            onLayoutPreferences={updateConsolePreferences}
            navigationTarget={navigationTarget}
            onNavigationResult={handleNavigationResult}
          />

          {st.stderrTail.length > 0 && (
            <div className="stderr-panel">
              <div className="stderr-head">:: stderr tail</div>
              {st.stderrTail.map((line, i) => <div className="stderr-line" key={i}>{line}</div>)}
            </div>
          )}

        </main>

        <div
          className="console-splitter console-splitter-vertical"
          role="separator"
          aria-label={uiText("Đổi kích thước panel Tầng và Kết quả", "Resize Stages and Results panel")}
          aria-orientation="vertical"
          aria-valuemin={0}
          aria-valuemax={CONSOLE_LAYOUT_LIMITS.rightMax}
          aria-valuenow={consoleLayout.rightCollapsed ? 0 : Math.round(consoleLayout.rightWidth)}
          tabIndex="0"
          title={uiText("Kéo để đổi chiều rộng Tầng/Kết quả; nhấp đúp để đặt lại", "Drag to resize Stages/Results; double-click to reset")}
          onPointerDown={event => beginConsoleResize("right", event)}
          onKeyDown={event => handleConsoleSplitterKey("right", event)}
          onDoubleClick={() => resetConsoleTrack("right")}
        />

        {/* ---------------- RIGHT ---------------- */}
        <aside className={"col col-right" + (consoleLayout.rightCollapsed ? " console-side-collapsed" : "")}>
          <button
            type="button"
            className="console-side-toggle console-side-toggle-right"
            onClick={() => toggleConsoleSide("right")}
            title={consoleLayout.rightCollapsed ? uiText("Mở Tầng và Kết quả", "Expand Stages and Results") : uiText("Thu gọn Tầng và Kết quả", "Collapse Stages and Results")}
            aria-label={consoleLayout.rightCollapsed ? uiText("Mở panel Tầng và Kết quả", "Expand Stages and Results panel") : uiText("Thu gọn panel Tầng và Kết quả", "Collapse Stages and Results panel")}
            aria-expanded={!consoleLayout.rightCollapsed}
          >
            {consoleLayout.rightCollapsed ? "‹" : "›"}
          </button>
          <div className="console-side-content">
          <div className="section-label">:: {uiText("các tầng", "stages")}</div>
          {consoleStagePlan.map((s, i) => {
            const si = st.stageInfo[s.id] || { status: "pending" };
            const stageAvailable = st.normalized.some(row => row.stage === s.id);
            let cls = "stage-pending", dot = "○", prog = "";
            const exactProgress = consoleWorkflowProgress(si.declaredProgress || s.progress);
            if (si.status === "done") { cls = "stage-done"; dot = "●"; prog = si.skipped ? uiText("đã bỏ qua", "skipped") : (exactProgress || uiText("xong", "done")); }
            else if (si.status === "active") {
              cls = "stage-active";
              dot = "●";
              prog = exactProgress || (s.id === "translator" && si.previews ? (translatorProgressLabel || `${si.previews} win`) : uiText("đang chạy", "running"));
            }
            else if (si.status === "failed") { cls = "stage-failed"; dot = "✕"; prog = uiText("thất bại", "failed"); }
            else if (s.optional && isTerminal) { cls = "stage-pending"; dot = "○"; prog = uiText("đã bỏ qua", "skipped"); }
            const prev = consoleStagePlan[i - 1];
            return (
              <React.Fragment key={s.id}>
                {prev && prev.phaseEnd && (
                  <div className="phase-divider">
                    <span className={prev.componentId ? "phase-ok" : st.phase1Done ? "phase-ok" : ""}>{prev.componentId || `PHASE ${prev.phase}`}{!prev.componentId && st.phase1Done ? " ✓" : ""}</span>
                    <span className="phase-line" />
                    <span>{s.componentId || `PHASE ${s.phase}`}</span>
                  </div>
                )}
                <div className={"stage-row " + cls + (highlightedStage === s.id ? " stage-targeted" : "")}>
                  <button
                    type="button"
                    className="stage-target-button"
                    disabled={!stageAvailable}
                    onClick={() => navigateToStage(s.id)}
                    title={stageAvailable ? `${consoleText(uiLocale, "navigationStageFilter")}: ${s.label}` : consoleText(uiLocale, "navigationTargetUnavailable")}
                  >
                    <span className="stage-dot">{dot}</span>
                    <span className="stage-name">{s.label}</span>
                  </button>
                  {si.skipped && si.skipReason
                    ? <ConsoleSkipReason reason={si.skipReason} locale={uiLocale} stageLabel={s.label} />
                    : <span className="stage-progress">{prog}</span>}
                  <span className="stage-eta">{consoleDuration(si.start, si.end)}</span>
                </div>
              </React.Fragment>
            );
          })}

          <ConsoleWorkflowEvidence
            workflowReplay={workflowReplay}
            uiText={uiText}
            selectedArtifact={selectedArtifact}
            onSelectArtifact={setSelectedArtifact}
          />

          <div className="section-label">:: {consoleText(uiLocale, "latestArtifact")}</div>
          <div
            className={"artifact-path" + (selectedArtifact ? " artifact-targeted" : "")}
            title={selectedArtifact || st.latestArtifact || ""}
          >
            {(selectedArtifact || st.latestArtifact)
              ? consoleBaseName(selectedArtifact || st.latestArtifact)
              : consoleText(uiLocale, "noneYet")}
          </div>

          <div className="section-label">:: {consoleText(uiLocale, "results")}</div>
          {workflowReplay ? (
            <div className="artifact-path kv-dim">
              {!workflowReplay.valid
                ? uiText("Report bị ẩn vì replay không hợp lệ.", "Report hidden because replay validation failed.")
                : workflowReplay.scoring?.reports?.length
                ? workflowReplay.scoring.reports.map(report => `${report.artifact_ref} · ${report.sha256}`).join("\n")
                : uiText("Không có report Evaluation đã được xác thực trong snapshot này; UI không suy ra điểm hoặc verdict.", "This snapshot has no validated Evaluation report; the UI does not infer scores or a verdict.")}
            </div>
          ) : visibleReportSummary && (visibleReportSummary.final?.present || visibleReportSummary.phase_1?.present) ? (
            <>
              {((visibleReportSummary.final?.present ? visibleReportSummary.final.metrics : visibleReportSummary.phase_1?.metrics) || []).map(m => (
                <div className="kv-row" key={m.key}>
                  <ConsoleMetricLabel
                    metricKey={m.key}
                    fallbackLabel={m.label || m.key}
                    locale={uiLocale}
                    idSuffix={`result-${m.key}`}
                  />
                  <span className={"kv-value " + (m.status === "good" ? "kv-good" : m.status === "warn" ? "kv-warn" : m.status === "bad" ? "kv-bad" : "")}>
                    {formatConsoleMetric(m.value, m.unit)}
                  </span>
                </div>
              ))}
              {finalGateText && (
                <div className="kv-row">
                  <span className="kv-label">{consoleText(uiLocale, "gates")}</span>
                  <span className={"kv-value " + (finalGate.all_ok === false ? "kv-bad" : "kv-good")}>{finalGateText}</span>
                </div>
              )}
              {compareGap && (
                <>
                  <div className="kv-row">
                    <ConsoleMetricLabel
                      metricKey="TC"
                      locale={uiLocale}
                      prefix={`${consoleText(uiLocale, "gap")} `}
                      suffix="(S1-S0)"
                      idSuffix="gap-tc"
                    />
                    <span className={"kv-value " + (Number(compareGap.TC) >= 0 ? "kv-good" : "kv-bad")}>{formatConsoleSignedRatio(compareGap.TC)}</span>
                  </div>
                  <div className="kv-row">
                    <ConsoleMetricLabel
                      metricKey="TA"
                      locale={uiLocale}
                      prefix={`${consoleText(uiLocale, "gap")} `}
                      suffix="(S1-S0)"
                      idSuffix="gap-ta"
                    />
                    <span className={"kv-value " + (Number(compareGap.TA) >= 0 ? "kv-good" : "kv-warn")}>{formatConsoleSignedRatio(compareGap.TA)}</span>
                  </div>
                </>
              )}
              {visibleReportSummary.final?.present && visibleReportSummary.final.verdict && typeof visibleReportSummary.final.verdict.pass === "boolean" && (
                <div className={"banner " + (visibleReportSummary.final.verdict.pass === false ? "banner-red" : "banner-green")}>
                  <span className="banner-glyph">{visibleReportSummary.final.verdict.pass === false ? "✕" : "●"}</span>
                  <span className="banner-msg">{visibleReportSummary.final.verdict.pass === false ? (consoleText(uiLocale, "gateFail") + " · " + ((visibleReportSummary.final.verdict.reasons || []).join(", ") || consoleText(uiLocale, "seeReport"))) : consoleText(uiLocale, "gatePass")}</span>
                </div>
              )}
              {visibleReportSummary.final?.report_path && <div className="artifact-path">{visibleReportSummary.final.report_path}</div>}
            </>
          ) : <div className="artifact-path kv-dim">{consoleText(uiLocale, "noScores")}</div>}

          </div>
        </aside>
      </div>
    </div>
  );
}

function uniqueConsole(arr) { return Array.from(new Set(arr)).sort(); }
function formatConsoleInt(n) { return Number(n || 0).toLocaleString(ThesisI18n.getLocale() === "en" ? "en-US" : "vi-VN"); }

/* Typewriter reveal for the translation ledger only (one effect, one section).
   Adaptive speed: aims for ~1.4s total regardless of length, floored so short
   lines still feel typed. Honors prefers-reduced-motion (shows full text). */
function useConsoleTypewriter(text) {
  const [shown, setShown] = React.useState(text || "");
  React.useEffect(() => {
    const full = text || "";
    const reduce = typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !full) { setShown(full); return; }
    const cps = Math.max(45, full.length / 1.4);
    const stepMs = 1000 / cps;
    let i = 0, timer = null, cancelled = false;
    setShown("");
    const tick = () => {
      if (cancelled) return;
      i = Math.min(full.length, i + Math.max(1, Math.round(cps / 40)));
      setShown(full.slice(0, i));
      if (i < full.length) timer = setTimeout(tick, stepMs);
    };
    timer = setTimeout(tick, stepMs);
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [text]);
  return shown;
}

function ConsoleTypewriter({ text }) {
  const shown = useConsoleTypewriter(text);
  const typing = shown.length < (text || "").length;
  return <p>{shown}{typing && <span className="typewriter-caret">▌</span>}</p>;
}

if (typeof window !== "undefined") {
  window.AgentConsoleView = AgentConsoleView;
}
