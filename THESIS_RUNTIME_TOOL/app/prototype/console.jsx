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
const CONSOLE_TERMINAL_STATUSES = new Set(["done", "failed", "cancelled", "canceled", "error"]);
const CONSOLE_RENDER_CAP = 2000;
const CONSOLE_INJECTED_TIERS = ["hard", "soft", "preserve"];
const CONSOLE_TIER_LABELS = {
  hard: "mandatory (hard)",
  soft: "soft",
  preserve: "preserve",
};

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

function consolePackContentRows(summary) {
  const sample = summary && typeof summary.sample === "object" && summary.sample ? summary.sample : {};
  const more = summary && typeof summary.more === "object" && summary.more ? summary.more : {};
  const buckets = [
    ["mandatory", "MAND"],
    ["soft", "SOFT"],
    ["preserve", "KEEP"],
    ["address", "ADDR"],
    ["quarantine", "QUAR"],
  ];
  const rows = [];
  buckets.forEach(([key, label]) => {
    const values = Array.isArray(sample[key]) ? sample[key].slice(0, 6) : [];
    values.forEach((line, index) => rows.push({ key: `${key}:${index}`, label, line: String(line) }));
    if (more[key]) rows.push({ key: `${key}:more`, label, line: `+${more[key]} more` });
  });
  return rows;
}

function consoleTierCount(row) {
  if (!row || !Number(row.terms)) return null;
  return `${Number(row.consistent_terms || 0)}/${Number(row.terms || 0)}`;
}

function consoleTierIsComplete(row) {
  return !!row && Number(row.terms || 0) > 0 && Number(row.consistent_terms || 0) === Number(row.terms || 0);
}

function consoleFormsSummary(forms, limit = 3) {
  const entries = Object.entries(forms || {})
    .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0) || String(a[0]).localeCompare(String(b[0])));
  if (!entries.length) return "none";
  return entries.slice(0, limit).map(([form, count]) => `"${form}" x${count}`).join(" / ");
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

function consoleConsistencyTierText(row) {
  if (row.s0 && row.s1) {
    const suffix = row.fixedCount ? ` (+${row.fixedCount} memory-fixed)` : "";
    return `${consoleTierCount(row.s0)} -> ${consoleTierCount(row.s1)}${consoleTierIsComplete(row.s1) ? " ✓" : ""}${suffix}`;
  }
  const single = row.s1 || row.s0 || row.fallback;
  const prefix = row.fallbackCfg ? row.fallbackCfg + " " : "";
  return `${prefix}${consoleTierCount(single) || "—"}`;
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
    case "stage_start": return `${ctx.label || row.stage} bắt đầu`;
    case "stage_done": return `${ctx.label || row.stage} xong · exit ${p.exit_code}`;
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
    case "artifact_created": return consoleBaseName(p.artifact_path) || "artifact";
    case "warning": return consoleShort(p.message || p.reason, 60);
    case "error": return consoleShort(p.error || p.message, 60);
    case "retry": return `retry ${p.attempt || ""} · ${p.reason || ""}`;
    default: return consoleShort(p.message || p.reason || row.event, 60);
  }
}

/* Pure: turn a raw merged event array into everything the console renders. */
function deriveConsoleState(events) {
  const stageInfo = {};
  CONSOLE_STAGE_PLAN.forEach(s => { stageInfo[s.id] = { status: "pending", start: null, end: null, exit: null, previews: 0 }; });
  let winCounter = 0;
  let cumulativeCost = null;
  let budgetCap = null;
  let warnings = 0, errors = 0;
  let phase1Done = false;
  let runStatus = "";
  let latestArtifact = null;
  let latestPreviewWin = null;
  let translatorTotal = null;
  let stderrTail = [];
  let paused = false, pausedReason = "";
  let latestPackSummary = null;
  let latestPackWindow = null;
  const normalized = [];

  events.forEach((raw, idx) => {
    const payload = raw && typeof raw.payload === "object" && raw.payload ? raw.payload : {};
    const stage = raw.stage || "";
    const event = raw.event || raw.event_type || "";
    // per-window counter (translator lifecycle) for readable messages
    if (event === "window_started") winCounter += 1;
    const ctxPlan = CONSOLE_STAGE_PLAN.find(s => s.id === stage);
    const ctx = { label: ctxPlan ? ctxPlan.label : stage, win: event.startsWith("window") || ["prompt_built", "request_sent", "response_received", "json_parsed", "persist_buffered"].includes(event) ? Math.max(winCounter, 1) : null };
    const severity = consoleEventSeverity(raw);
    if (severity === "warning") warnings += 1;
    if (severity === "error") errors += 1;

    if (stageInfo[stage]) {
      const si = stageInfo[stage];
      if (event === "stage_start") { si.status = "active"; si.start = raw.ts; }
      else if (event === "stage_done") { si.status = payload.exit_code === 0 ? "done" : "failed"; si.end = raw.ts; si.exit = payload.exit_code; }
      else if (event === "stage_skipped") { si.status = "done"; si.skipped = true; }
      else if (event === "window_preview_available") { si.previews += 1; }
      if (severity === "error") si.status = "failed";
    }
    const progressTotal = payload.progress && payload.progress.total != null ? Number(payload.progress.total) : null;
    const payloadTotal = payload.total_windows ?? payload.windows_total ?? payload.window_total ?? progressTotal;
    if (stage === "translator" && payloadTotal != null && Number.isFinite(Number(payloadTotal))) {
      translatorTotal = Math.max(Number(translatorTotal || 0), Number(payloadTotal));
    }
    if (event === "window_preview_available") latestPreviewWin = Math.max(winCounter, 1);
    if ((event === "prompt_built" || event === "pack_built") && payload.pack_summary) {
      latestPackSummary = payload.pack_summary;
      latestPackWindow = payload.window_id || (ctx.win ? `window ${ctx.win}` : "");
    }
    if (event === "checkpoint" && payload.checkpoint === "phase_1_done") phase1Done = true;
    if (event === "cost_snapshot") {
      if (payload.estimated_cumulative_usd != null) cumulativeCost = Number(payload.estimated_cumulative_usd);
      if (payload.budget_cap_usd != null) budgetCap = Number(payload.budget_cap_usd);
    }
    if (event === "gate_pause") {
      paused = true;
      pausedReason = payload.reason || "gate";
    } else if (paused && event === "stage_start") {
      paused = false;
      pausedReason = "";
    }
    if (event === "artifact_created" && payload.artifact_path) latestArtifact = payload.artifact_path;
    if (event === "stage_done" && payload.artifact_path) latestArtifact = payload.artifact_path;
    if (event === "run_done") runStatus = payload.status || "done";
    if (event === "run_failed") { runStatus = "failed"; if (payload.error) stderrTail = String(payload.error).split("\n").slice(-4); }
    if (event === "error" && payload.error) stderrTail = String(payload.error).split("\n").slice(-4);

    normalized.push({
      key: raw.event_id || `${raw.seq}:${idx}`,
      ts: raw.ts || "",
      stage,
      agent: raw.agent || "",
      event,
      severity,
      seq: raw.seq,
      attempt: raw.attempt_id,
      lineNo: idx + 1,
      glyph: CONSOLE_SEVERITY_GLYPH[severity === "info" && event === "cost_snapshot" ? "context" : severity] || "├",
      isCost: event === "cost_snapshot",
      isContext: event === "gate_pause",
      dur: payload.duration_s || payload.latency_s || null,
      message: consoleMessageFor(raw, ctx),
    });
  });

  const stagesSeen = CONSOLE_STAGE_PLAN.filter(s => stageInfo[s.id].status !== "pending").length;
  const lastTs = normalized.length ? normalized[normalized.length - 1].ts : "";
  // llm-ish activity from the translator lifecycle (real runs have no llm_call yet)
  const llmCalls = normalized.filter(r => r.event === "request_sent" || r.event === "llm_call").length;

  return {
    normalized, stageInfo, stagesSeen, cumulativeCost, budgetCap,
    warnings, errors, phase1Done, runStatus, latestArtifact, latestPreviewWin,
    stderrTail, paused, pausedReason, lastTs, llmCalls,
    translatorTotal, latestPackSummary, latestPackWindow,
    totalEvents: normalized.length,
  };
}

function consoleAgeSeconds(ts) {
  if (!ts) return Infinity;
  const t = Date.parse(ts);
  if (isNaN(t)) return Infinity;
  return (Date.now() - t) / 1000;
}

function AgentConsoleView(props) {
  const {
    runId, runs = [], onSelectRun,
    events = [], running = false, status = "",
    truncated = false, partialLine = false,
    blockPreview = [], watchlist = [],
    reportSummary = null,
    theme = "paper", onToggleTheme,
    onRefresh, onPause, onCancel, onResume, onDich, busy = false,
  } = props;

  const [stageFilter, setStageFilter] = React.useState("");
  const [agentFilter, setAgentFilter] = React.useState("");
  const [severityFilter, setSeverityFilter] = React.useState("");
  // Client-side replay: reveal saved events over time so a finished run animates
  // (stages progress, cost climbs, typewriter fires). No backend, no new run — the
  // real block-preview/watchlist stay attached to this same run.
  const [replayN, setReplayN] = React.useState(null);
  const replayTimer = React.useRef(null);
  const stopReplay = React.useCallback(() => {
    if (replayTimer.current) { clearInterval(replayTimer.current); replayTimer.current = null; }
  }, []);
  React.useEffect(() => () => stopReplay(), [stopReplay]);
  React.useEffect(() => { setReplayN(null); stopReplay(); }, [runId, stopReplay]);
  function startReplay() {
    if (!events.length) return;
    stopReplay();
    let i = 0;
    const step = Math.max(1, Math.round(events.length / 60));
    setReplayN(0);
    replayTimer.current = setInterval(() => {
      i += step;
      if (i >= events.length) { setReplayN(null); stopReplay(); }
      else setReplayN(i);
    }, 220);
  }
  const replaying = replayN != null;
  const shownEvents = replaying ? events.slice(0, replayN) : events;

  const st = React.useMemo(() => deriveConsoleState(shownEvents), [shownEvents]);
  const runStatus = st.runStatus || status || (running ? "running" : "idle");
  const hasRun = !!runId;
  const isTerminal = hasRun && consoleIsTerminalStatus(runStatus);
  const isOpenRun = hasRun && !isTerminal;
  const stalled = isOpenRun && running && consoleAgeSeconds(st.lastTs) > 90;
  const canResumeRun = !!onResume && (runStatus === "failed" || st.paused || stalled);

  const agents = uniqueConsole(st.normalized.map(r => r.agent).filter(Boolean));
  const severities = uniqueConsole(st.normalized.map(r => r.severity).filter(Boolean));
  const filtered = st.normalized.filter(r =>
    (!stageFilter || r.stage === stageFilter)
    && (!agentFilter || r.agent === agentFilter)
    && (!severityFilter || r.severity === severityFilter));
  const hiddenOlderEvents = Math.max(0, filtered.length - CONSOLE_RENDER_CAP);
  const rendered = filtered.slice(-CONSOLE_RENDER_CAP).reverse();
  const memoryRows = consolePackContentRows(st.latestPackSummary);

  const costPct = st.budgetCap ? Math.min(100, Math.round((st.cumulativeCost / st.budgetCap) * 100)) : 0;
  const healthLabel = stalled ? "stalled" : running ? "running" : isTerminal ? runStatus : isOpenRun ? (runStatus || "connecting") : "quiet";
  const healthClass = stalled || runStatus === "failed" ? "kv-bad" : running ? "kv-good" : isOpenRun ? "kv-warn" : "kv-dim";
  const statusChipClass = runStatus === "failed" ? "hdr-status-bad" : runStatus === "done" ? "hdr-status-good" : stalled || st.paused || (isOpenRun && !running) ? "hdr-status-warn" : running ? "hdr-status-good" : "";
  const reportCfgs = (reportSummary && reportSummary.phase_1 && reportSummary.phase_1.configs) || [];
  const isCompareRun = reportCfgs.includes("S0") || !!(reportSummary && reportSummary.compare && reportSummary.compare.present);
  const armsLabel = reportCfgs.length ? (isCompareRun ? "S0+S1" : reportCfgs.join("+")) : (isCompareRun ? "S0+S1" : null);
  const compareGap = reportSummary && reportSummary.compare && reportSummary.compare.present ? (reportSummary.compare.gap || {}) : null;
  const finalGate = reportSummary && reportSummary.final && reportSummary.final.stage_gate && reportSummary.final.stage_gate.present ? reportSummary.final.stage_gate : null;
  const finalGateText = formatConsoleGate(finalGate);
  const consistencySummary = reportSummary && reportSummary.consistency && reportSummary.consistency.present ? reportSummary.consistency : null;
  const consistencyTierRows = consoleConsistencyTierRows(consistencySummary);
  const consistencyNotable = (consistencySummary && consistencySummary.notable_terms) || [];
  const consistencyFixedTerms = consistencyNotable.filter(item => item.fixed_by_injection);
  const consistencyResidualDrift = consistencyNotable.filter(item => {
    const s1 = item.by_config && item.by_config.S1;
    return s1 && s1.status && s1.status !== "consistent" && !item.fixed_by_injection;
  });

  // latest translated window preview (text lives in blockPreview prop, not events)
  const previewIdx = st.latestPreviewWin ? Math.min(st.latestPreviewWin, blockPreview.length) - 1 : blockPreview.length - 1;
  const preview = previewIdx >= 0 ? blockPreview[previewIdx] : null;

  return (
    <div className={"agentconsole console-theme-" + theme}>
      <header className="console-header">
        <span className="brand">⬢ AGENT CONSOLE</span>
        <select className="run-picker" aria-label="Run picker" value={runId || ""} onChange={e => onSelectRun && onSelectRun(e.target.value)}>
          {!runId && <option value="">select run</option>}
          {runs.slice(0, 40).map(r => {
            const t = r.started_at ? new Date(r.started_at) : null;
            const stamp = t && !isNaN(t.getTime())
              ? " · " + String(t.getMonth()+1).padStart(2,"0") + "-" + String(t.getDate()).padStart(2,"0")
                + " " + String(t.getHours()).padStart(2,"0") + ":" + String(t.getMinutes()).padStart(2,"0")
              : "";
            return <option key={r.run_id} value={r.run_id}>{r.run_id}{r.status ? " · " + r.status : ""}{stamp}</option>;
          })}
        </select>
        <span className="hdr-actions">
          {onDich && <button className="btn btn-accent" type="button" disabled={busy || isOpenRun} onClick={onDich} title="Chạy toàn bộ pipeline one-button cho dataset đang mở">▸ DỊCH</button>}
          {events.length > 0 && !isOpenRun && <button className="btn" type="button" onClick={startReplay} title="Phát lại event stream theo thời gian (client-side, $0)">{replaying ? "▶ replaying…" : "▶ replay"}</button>}
          <button className="btn" type="button" disabled={busy} onClick={onRefresh}>↻ refresh</button>
          <button className="btn" type="button" disabled={!isOpenRun || !onPause} onClick={onPause}>⏸ pause after stage</button>
          {canResumeRun
            ? <button className="btn btn-accent" type="button" onClick={onResume}>▸ resume</button>
            : <button className="btn btn-danger" type="button" disabled={!isOpenRun || !onCancel} onClick={onCancel}>✕ cancel</button>}
          <button className="btn" type="button" onClick={onToggleTheme}>◐ theme</button>
          <span className={"hdr-status " + statusChipClass}>{stalled ? "stalled" : runStatus}</span>
          {armsLabel && <span className={"hdr-status " + (isCompareRun ? "hdr-status-good" : "")} title={isCompareRun ? "Chạy cả S0 và S1 (có so sánh)" : "Chỉ S1"}>{armsLabel}</span>}
        </span>
      </header>

      <div className="console-body">
        {/* ---------------- LEFT ---------------- */}
        <aside className="col col-left">
          <div className="section-label">:: overview</div>
          <div className="kv-row"><span className="kv-label">status</span><span className={"kv-value " + (runStatus === "failed" ? "kv-bad" : runStatus === "done" ? "kv-good" : "")}>{runStatus}</span></div>
          {armsLabel && <div className="kv-row"><span className="kv-label">arms</span><span className={"kv-value " + (isCompareRun ? "kv-good" : "kv-dim")}>{armsLabel}</span></div>}
          <div className="kv-row"><span className="kv-label">stages seen</span><span className="kv-value">{st.stagesSeen} / {CONSOLE_STAGE_PLAN.length}</span></div>
          <div className="kv-row"><span className="kv-label">events</span><span className="kv-value">{formatConsoleInt(st.totalEvents)}</span></div>
          <div className="kv-row"><span className="kv-label">stream</span><span className="kv-value kv-dim">{truncated ? "truncated" : partialLine ? "partial line" : isOpenRun ? (running ? "live" : "connecting") : "closed"}</span></div>

          <div className="section-label">:: cost &amp; cache</div>
          <div className="kv-row"><span className="kv-label">cap total</span><span className="kv-value">{st.cumulativeCost != null ? "$" + st.cumulativeCost.toFixed(4) : "—"}</span></div>
          <div className="kv-row kv-row-bar"><span className="kv-label">cap / budget</span><span className="kv-value kv-dim">{st.cumulativeCost != null ? "$" + st.cumulativeCost.toFixed(3) : "—"} / {st.budgetCap != null ? "$" + st.budgetCap.toFixed(2) : "—"}</span></div>
          <div className="bar"><div className="bar-fill" style={{ width: costPct + "%" }} /></div>
          <div className="kv-row"><span className="kv-label">llm events</span><span className="kv-value">{formatConsoleInt(st.llmCalls)}</span></div>

          <div className="section-label">:: health</div>
          <div className="kv-row"><span className="kv-label">state</span><span className={"kv-value " + healthClass}>{healthLabel}</span></div>
          <div className="kv-row"><span className="kv-label">warnings</span><span className={"kv-value " + (st.warnings ? "kv-warn" : "")}>{formatConsoleInt(st.warnings)}</span></div>
          <div className="kv-row"><span className="kv-label">errors</span><span className={"kv-value " + (st.errors ? "kv-bad" : "")}>{formatConsoleInt(st.errors)}</span></div>
          <div className="kv-row"><span className="kv-label">last event</span><span className="kv-value kv-dim">{st.lastTs ? st.lastTs.slice(11, 19) : "—"}</span></div>
        </aside>

        {/* ---------------- MAIN ---------------- */}
        <main className="col col-main">
          {runStatus === "failed" && (
            <div className="banner banner-red">
              <span className="banner-glyph">✕</span>
              <span className="banner-msg">Run failed{st.stderrTail.length ? " · " + consoleShort(st.stderrTail[st.stderrTail.length - 1], 70) : ""}</span>
              {onResume && <span className="banner-actions"><button className="btn btn-mini" onClick={onResume}>resume</button></span>}
            </div>
          )}
          {st.paused && !isTerminal && runStatus !== "failed" && (
            <div className="banner banner-amber">
              <span className="banner-glyph">⏸</span>
              <span className="banner-msg">Đã dừng · {st.pausedReason} — resume để chạy tiếp</span>
              {onResume && <span className="banner-actions"><button className="btn btn-mini" onClick={onResume}>resume</button></span>}
            </div>
          )}
          {st.phase1Done && !isTerminal && !st.paused && (
            <div className="banner banner-green">
              <span className="banner-glyph">●</span>
              <span className="banner-msg">Phase 1 xong — bản dịch + báo cáo nhanh đã sẵn sàng</span>
            </div>
          )}
          {stalled && (
            <div className="banner banner-red">
              <span className="banner-glyph badge-stalled">▲</span>
              <span className="banner-msg">Không có sự kiện {Math.round(consoleAgeSeconds(st.lastTs))}s — có thể treo</span>
              {onResume && <span className="banner-actions"><button className="btn btn-mini" onClick={onResume}>resume</button></span>}
            </div>
          )}

          <div className="section-label">:: event stream</div>
          <div className="filterbar">
            <select className="filter-select" value={stageFilter} onChange={e => setStageFilter(e.target.value)}>
              <option value="">stage: all</option>
              {CONSOLE_STAGE_PLAN.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
            <select className="filter-select" value={agentFilter} onChange={e => setAgentFilter(e.target.value)}>
              <option value="">agent: all</option>
              {agents.map(a => <option key={a} value={a}>{a}</option>)}
            </select>
            <select className="filter-select" value={severityFilter} onChange={e => setSeverityFilter(e.target.value)}>
              <option value="">severity: all</option>
              {severities.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            <span className="filter-count">{formatConsoleInt(rendered.length)} / {formatConsoleInt(filtered.length)} shown</span>
          </div>

          <div className="event-feed">
            {rendered.length ? rendered.map(r => (
              <div key={r.key} className={"ev-row ev-" + r.severity + (r.isCost ? " ev-cost" : "") + (r.isContext ? " ev-context" : "")}>
                <span className="ev-time">{r.ts ? r.ts.slice(11, 19) : "--:--:--"}</span>
                <span className="ev-glyph">{r.glyph}</span>
                <span className="ev-body">
                  <b className="ev-type">{r.event}</b>
                  <span className="ev-src">{r.stage || "-"}{r.agent ? " · " + r.agent : ""}</span>
                  <span className="ev-msg">{r.message}</span>
                  {r.dur != null && <span className="ev-dur">({r.dur}s)</span>}
                </span>
                <span className="ev-seq">#{r.lineNo != null ? r.lineNo : "-"}{r.attempt != null && r.seq != null ? ` · a${r.attempt}/${r.seq}` : ""}</span>
              </div>
            )) : <div className="console-empty">Chọn hoặc replay một run để xem dòng sự kiện.</div>}
            {hiddenOlderEvents > 0 && (
              <div className="console-empty">... {formatConsoleInt(hiddenOlderEvents)} dòng cũ hơn ẩn - dùng filter để thu hẹp.</div>
            )}
          </div>

          {st.stderrTail.length > 0 && (
            <div className="stderr-panel">
              <div className="stderr-head">:: stderr tail</div>
              {st.stderrTail.map((line, i) => <div className="stderr-line" key={i}>{line}</div>)}
            </div>
          )}

          <div className="latest-block">
            <div className="latest-block-head">
              <span className="latest-block-id">latest block · {preview ? preview.block_id : (st.latestPreviewWin ? "window " + st.latestPreviewWin : "—")}</span>
              <span className="latest-block-tag">{preview ? (preview.model || "translated") : "no preview"}</span>
            </div>
            <div className="typewriter-target">
              {preview
                ? <ConsoleTypewriter text={consoleShort(preview.target_text, 220)} />
                : <p className="kv-dim">Bản dịch preview lấy theo block_id (translation_runs.output_text) khi có.</p>}
            </div>
          </div>
        </main>

        {/* ---------------- RIGHT ---------------- */}
        <aside className="col col-right">
          <div className="section-label">:: stages</div>
          {CONSOLE_STAGE_PLAN.map((s, i) => {
            const si = st.stageInfo[s.id] || { status: "pending" };
            let cls = "stage-pending", dot = "○", prog = "";
            if (si.status === "done") { cls = "stage-done"; dot = "●"; prog = si.skipped ? "skipped" : "done"; }
            else if (si.status === "active") {
              cls = "stage-active";
              dot = "●";
              const total = st.translatorTotal || blockPreview.length || null;
              prog = s.id === "translator" && si.previews ? si.previews + "/" + (total || "?") + " win" : "running";
            }
            else if (si.status === "failed") { cls = "stage-failed"; dot = "✕"; prog = "failed"; }
            else if (s.optional && isTerminal) { cls = "stage-pending"; dot = "○"; prog = "skipped"; }
            const prev = CONSOLE_STAGE_PLAN[i - 1];
            return (
              <React.Fragment key={s.id}>
                {prev && prev.phaseEnd && (
                  <div className="phase-divider"><span className={st.phase1Done ? "phase-ok" : ""}>PHASE 1{st.phase1Done ? " ✓" : ""}</span><span className="phase-line" /><span>PHASE 2</span></div>
                )}
                <div className={"stage-row " + cls}>
                  <span className="stage-dot">{dot}</span>
                  <span className="stage-name">{s.label}</span>
                  <span className="stage-progress">{prog}</span>
                  <span className="stage-eta">{consoleDuration(si.start, si.end)}</span>
                </div>
              </React.Fragment>
            );
          })}

          <div className="section-label">:: latest artifact</div>
          <div className="artifact-path">{st.latestArtifact ? consoleBaseName(st.latestArtifact) : "none yet"}</div>

          <div className="section-label">:: memory content{st.latestPackWindow ? " · " + st.latestPackWindow : ""}</div>
          {memoryRows.length ? memoryRows.map(row => (
            <div className="watch-row" key={row.key}>
              <span className="watch-term">{row.label}</span>
              <span className="watch-arrow">·</span>
              <span className="watch-vi">{consoleShort(row.line, 46)}</span>
            </div>
          )) : <div className="artifact-path kv-dim">Chưa có pack sample trong event.</div>}

          <div className="section-label">:: results</div>
          {reportSummary && (reportSummary.final?.present || reportSummary.phase_1?.present) ? (
            <>
              {((reportSummary.final?.present ? reportSummary.final.metrics : reportSummary.phase_1?.metrics) || []).map(m => (
                <div className="kv-row" key={m.key}>
                  <span className="kv-label">{m.label || m.key}</span>
                  <span className={"kv-value " + (m.status === "good" ? "kv-good" : m.status === "warn" ? "kv-warn" : m.status === "bad" ? "kv-bad" : "")}>
                    {formatConsoleMetric(m.value, m.unit)}
                  </span>
                </div>
              ))}
              {finalGateText && (
                <div className="kv-row">
                  <span className="kv-label">gates</span>
                  <span className={"kv-value " + (finalGate.all_ok === false ? "kv-bad" : "kv-good")}>{finalGateText}</span>
                </div>
              )}
              {compareGap && (
                <>
                  <div className="kv-row">
                    <span className="kv-label">gap TC (S1-S0)</span>
                    <span className={"kv-value " + (Number(compareGap.TC) >= 0 ? "kv-good" : "kv-bad")}>{formatConsoleSignedRatio(compareGap.TC)}</span>
                  </div>
                  <div className="kv-row">
                    <span className="kv-label">gap TA (S1-S0)</span>
                    <span className={"kv-value " + (Number(compareGap.TA) >= 0 ? "kv-good" : "kv-warn")}>{formatConsoleSignedRatio(compareGap.TA)}</span>
                  </div>
                </>
              )}
              {reportSummary.final?.present && reportSummary.final.verdict && typeof reportSummary.final.verdict.pass === "boolean" && (
                <div className={"banner " + (reportSummary.final.verdict.pass === false ? "banner-red" : "banner-green")}>
                  <span className="banner-glyph">{reportSummary.final.verdict.pass === false ? "✕" : "●"}</span>
                  <span className="banner-msg">{reportSummary.final.verdict.pass === false ? ("Gate FAIL · " + ((reportSummary.final.verdict.reasons || []).join(", ") || "see report")) : "Gate PASS"}</span>
                </div>
              )}
              {reportSummary.final?.report_path && <div className="artifact-path">{reportSummary.final.report_path}</div>}
            </>
          ) : <div className="artifact-path kv-dim">Chưa có điểm — hiện sau khi score chạy xong.</div>}

          {consistencySummary && (
            <>
              <div className="section-label">:: consistency (memory -&gt; render)</div>
              {consistencyTierRows.length ? consistencyTierRows.map(row => {
                const s1Complete = row.s1 ? consoleTierIsComplete(row.s1) : consoleTierIsComplete(row.fallback);
                return (
                  <div className="kv-row" key={row.tier}>
                    <span className="kv-label">{CONSOLE_TIER_LABELS[row.tier] || row.tier}</span>
                    <span className={"kv-value " + (s1Complete ? "kv-good" : "kv-warn")}>{consoleConsistencyTierText(row)}</span>
                  </div>
                );
              }) : <div className="artifact-path kv-dim">Không có tier injection trong báo cáo.</div>}

              {consistencyFixedTerms.length > 0 && (
                <>
                  <div className="artifact-path kv-good">memory-fixed</div>
                  {consistencyFixedTerms.slice(0, 6).map(item => {
                    const s0 = item.by_config && item.by_config.S0;
                    const s1 = item.by_config && item.by_config.S1;
                    return (
                      <div className="watch-row" key={"fixed:" + item.source_term}>
                        <span className="watch-term">{consoleShort(item.source_term, 26)}</span>
                        <span className="watch-arrow">→</span>
                        <span className="watch-vi">{consoleShort(`S0: ${consoleFormsSummary(s0 && s0.forms)} -> S1: ${consoleFormsSummary(s1 && s1.forms)} ✓`, 64)}</span>
                      </div>
                    );
                  })}
                </>
              )}

              {consistencyResidualDrift.length > 0 && (
                <>
                  <div className="artifact-path kv-dim">residual drift</div>
                  {consistencyResidualDrift.slice(0, 6).map(item => {
                    const s1 = item.by_config && item.by_config.S1;
                    const soft = item.tier === "soft";
                    return (
                      <div className="watch-row" key={"residual:" + item.source_term}>
                        <span className="watch-term">{consoleShort(item.source_term, 22)} [{item.tier}]</span>
                        <span className="watch-arrow">→</span>
                        <span className={"watch-vi " + (soft ? "kv-warn" : "kv-bad")}>{consoleShort(`${soft ? "lệch được phép (do-not-force)" : "cần xem"}: ${consoleFormsSummary(s1 && s1.forms)}`, 56)}</span>
                      </div>
                    );
                  })}
                </>
              )}
            </>
          )}

          <div className="section-label">:: watchlist §36{watchlist.length ? " · " + watchlist.length + " pending / held" : ""}</div>
          {watchlist.length ? watchlist.slice(0, 8).map((w, i) => {
            const source = w.term || w.source_term || w.surface || w.source || "term";
            const target = w.vi || w.canonical_target_vi || w.target || w.canonical || "?";
            const candidatesLine = consoleWatchCandidatesLine(w);
            const evidenceLine = consoleWatchEvidenceLine(w);
            return (
              <React.Fragment key={w.entry_id || source || i}>
                <div className="watch-row">
                  <span className="watch-term">{consoleShort(source, 28)}</span>
                  <span className="watch-arrow">→</span>
                  <span className="watch-vi">{consoleShort(target, 28)}</span>
                </div>
                <div className="watch-row">
                  <span className="watch-term kv-warn">{consoleShort(consoleWatchReasonLabel(w), 24)}</span>
                  <span className="watch-arrow">·</span>
                  <span className="watch-vi kv-dim">{consoleShort(consoleWatchInjectionLabel(w), 34)}</span>
                </div>
                {candidatesLine && (
                  <div className="watch-row">
                    <span className="watch-term kv-dim">candidates</span>
                    <span className="watch-arrow">·</span>
                    <span className="watch-vi">{consoleShort(candidatesLine, 70)}</span>
                  </div>
                )}
                {evidenceLine && (
                  <div className="watch-row">
                    <span className="watch-term kv-dim">evidence</span>
                    <span className="watch-arrow">·</span>
                    <span className="watch-vi kv-dim">{evidenceLine}</span>
                  </div>
                )}
              </React.Fragment>
            );
          }) : <div className="artifact-path kv-dim">trống — nối sau bước re-election</div>}
        </aside>
      </div>
    </div>
  );
}

function uniqueConsole(arr) { return Array.from(new Set(arr)).sort(); }
function formatConsoleInt(n) { return Number(n || 0).toLocaleString("en-US"); }

/* Typewriter reveal for the latest-block preview only (one effect, one section).
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
