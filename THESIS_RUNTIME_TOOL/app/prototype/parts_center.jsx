/* ===== CENTER: block editor + continuous chapter stream ===== */

/* compute char offsets of current selection within a container element */
function selectionOffsets(container) {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!container.contains(range.commonAncestorContainer)) return null;
  const pre = range.cloneRange();
  pre.selectNodeContents(container);
  pre.setEnd(range.startContainer, range.startOffset);
  const start = pre.toString().length;
  const text = range.toString();
  if (!text.trim()) return null;
  return { start, end: start + text.length, text };
}

/* split text into segments by spans (non-overlapping; later spans skipped if overlapping) */
function segmentize(text, spans) {
  const sorted = [...spans].sort((a, b) => a.start - b.start);
  const segs = [];
  let cur = 0;
  for (const s of sorted) {
    if (s.start < cur || s.start >= text.length) continue;
    if (s.start > cur) segs.push({ text: text.slice(cur, s.start) });
    segs.push({ text: text.slice(s.start, Math.min(s.end, text.length)), span: s });
    cur = Math.min(s.end, text.length);
  }
  if (cur < text.length) segs.push({ text: text.slice(cur) });
  return segs;
}

function EditorToolbar({
  block, docInfo, reviewed, mode, streamLabel, streamCount, onNextUnreviewed,
  onChangeType, onToggleOpening, onToggleFlag, onMarkReviewed, readOnly
}) {
  const [typeOpen, setTypeOpen] = React.useState(false);
  const [flagOpen, setFlagOpen] = React.useState(false);
  const [detailsOpen, setDetailsOpen] = React.useState(false);
  const [legendOpen, setLegendOpen] = React.useState(false);
  const readOnlyPreview = mode === "preview" || readOnly;
  const consoleLike = mode === "console" || mode === "report";
  if (consoleLike) return null;
  const qualityFlags = block.quality_flags || [];
  const flags = qualityFlags.filter(f => f !== "ok");
  const editableBlock = mode === "block" && !readOnlyPreview;
  const contextTitle = mode === "preview"
    ? uiText("Kết quả dịch", "Translation Results")
    : mode === "block" ? block.block_id : streamLabel;
  const contextMeta = mode === "block"
    ? `${streamLabel} · ${block.block_type}`
    : `${streamCount || 0} block`;
  const provenance = [
    docInfo?.schema_version ? `schema ${docInfo.schema_version}` : "",
    docInfo?.metadata?.pipeline_version ? `pipeline ${docInfo.metadata.pipeline_version}` : "",
  ].filter(Boolean).join(" / ") || "extracted";
  return (
    <div className="ed-toolbar">
      <div className="ed-tb-left">
        <span className="ed-context">
          <b>{contextTitle}</b>
          <span>{contextMeta}</span>
        </span>

        {/* block_type dropdown */}
        {editableBlock && <div className="dd">
          <button className="dd-btn" onClick={() => { setTypeOpen(o => !o); setFlagOpen(false); setDetailsOpen(false); setLegendOpen(false); }}>
            <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
            <Ic.chevDown size={11} className="faint" />
          </button>
          {typeOpen && (<>
            <div className="menu-scrim" onClick={() => setTypeOpen(false)} />
            <div className="dd-menu">
              {DATA.BLOCK_TYPES.map(t => (
                <button key={t} className={"dd-item" + (t === block.block_type ? " cur" : "")}
                  onClick={() => { onChangeType(t); setTypeOpen(false); }}>
                  <span className={"tag tag-" + t}>{t}</span>
                  {t === block.block_type && <Ic.check size={12} className="dd-cur" />}
                </button>
              ))}
            </div>
          </>)}
        </div>}

        {/* chapter opening toggle */}
        {editableBlock && <button className={"tog" + (block.is_chapter_opening ? " on" : "")} onClick={onToggleOpening}>
          <span className="tog-sw"><span className="tog-knob" /></span>
          <Ic.bolt size={12} />{uiText("mở đầu chương", "chapter opening")}
        </button>}

        {/* quality flags */}
        {editableBlock && <div className="dd">
          <button className="dd-btn flags-btn" onClick={() => { setFlagOpen(o => !o); setTypeOpen(false); setDetailsOpen(false); setLegendOpen(false); }}>
            <Ic.flag size={12} className={flags.length ? "flag-on" : "faint"} />
            {flags.length === 0
              ? <span className="faint" style={{ fontSize: 12 }}>{uiText("không có cờ", "no flags")}</span>
              : flags.map(f => <span key={f} className="flag-chip">{f}</span>)}
            <Ic.chevDown size={11} className="faint" />
          </button>
          {flagOpen && (<>
            <div className="menu-scrim" onClick={() => setFlagOpen(false)} />
            <div className="dd-menu wide">
              <div className="dd-menu-head">quality_flags</div>
              {DATA.QUALITY_FLAGS.map(f => {
                const on = f === "ok" ? flags.length === 0 : qualityFlags.includes(f);
                return (
                  <button key={f} className={"dd-check" + (on ? " on" : "")} onClick={() => onToggleFlag(f)}>
                    <span className="dd-box">{on && <Ic.checkSmall size={10} />}</span>
                    <span className="mono">{f}</span>
                  </button>
                );
              })}
            </div>
          </>)}
        </div>}

        {readOnly && mode !== "preview" && <div className="dd">
          <button className="dd-btn" onClick={() => { setLegendOpen(open => !open); setDetailsOpen(false); }}>
            <Ic.layers size={12} />{uiText("Đánh dấu", "Highlights")}<Ic.chevDown size={11} className="faint" />
          </button>
          {legendOpen && (<>
            <div className="menu-scrim" onClick={() => setLegendOpen(false)} />
            <div className="dd-menu context-legend-menu"><OverlayLegend /></div>
          </>)}
        </div>}

        <div className="dd">
          <button className="dd-btn context-details-btn" onClick={() => { setDetailsOpen(open => !open); setLegendOpen(false); }}>
            <Ic.doc size={12} />{uiText("Chi tiết", "Details")}<Ic.chevDown size={11} className="faint" />
          </button>
          {detailsOpen && (<>
            <div className="menu-scrim" onClick={() => setDetailsOpen(false)} />
            <div className="dd-menu wide context-details-menu">
              <div><span>block_id</span><b className="mono">{block.block_id}</b></div>
              <div><span>chapter_id</span><b className="mono">{block.chapter_id}</b></div>
              <div><span>order_index</span><b className="mono">{String(block.order_index)}</b></div>
              <div><span>{uiText("nguồn gốc", "provenance")}</span><b className="mono">{provenance}</b></div>
            </div>
          </>)}
        </div>
      </div>

      <div className="ed-tb-right">
        {!readOnlyPreview && mode !== "block" && (
          <button className="btn sm" onClick={onNextUnreviewed}>
            <Ic.arrowRight size={13} />{uiText("Mục chưa duyệt tiếp theo", "Next unreviewed")}
          </button>
        )}
        {editableBlock && <button className={"btn sm reviewed-btn" + (reviewed ? " is-on" : "")} onClick={onMarkReviewed}>
          <Ic.checkCircle size={13} />{reviewed ? uiText("Đã duyệt", "Reviewed") : uiText("Đánh dấu đã duyệt", "Mark reviewed")}
        </button>}
      </div>
    </div>
  );
}

function SelectionPopover({ rect, onGlossary, onEntity }) {
  if (!rect) return null;
  return (
    <div
      className="sel-pop"
      style={{ top: rect.top, left: rect.left }}
      onMouseDown={e => {
        e.preventDefault();
        e.stopPropagation();
      }}
      onClick={e => e.stopPropagation()}
    >
      <button className="sel-pop-btn" onClick={onGlossary}><Ic.tag size={12} />{uiText("Thêm thuật ngữ", "Add glossary term")}</button>
      <div className="sel-pop-div" />
      <button className="sel-pop-btn" onClick={onEntity}><Ic.users size={12} />{uiText("Thêm lần nhắc thực thể", "Add entity mention")}</button>
    </div>
  );
}

function spanFocusId(span) {
  return String(span?.id || span?.term_id || span?.glossary_id || span?.source_term || "");
}

function SpanText({
  text, spans = [], block, onHoverSpan, onLeaveSpan, activeHighlightId,
  focusedTermId, onFocusSpan,
}) {
  return (
    <>
      {segmentize(text || "", spans).map((seg, i) => {
        const focusId = seg.span ? spanFocusId(seg.span) : "";
        const focused = !!focusedTermId && focusId === focusedTermId;
        const dimmed = !!focusedTermId && !!focusId && focusId !== focusedTermId;
        return seg.span
          ? <mark key={i}
              className={
                "hl hl-" + seg.span.kind +
                (seg.span.stale ? " hl-stale" : "") +
                (seg.span.status ? " hl-status-" + seg.span.status : "") +
                (activeHighlightId && activeHighlightId === seg.span.id ? " hl-linked" : "") +
                (focused ? " hl-focus-match" : "") +
                (dimmed ? " hl-focus-dim" : "")
              }
              aria-label={seg.span.label}
              data-focus-id={focusId || undefined}
              data-focus-target={seg.span.target ? "1" : undefined}
              data-focus-surface={seg.span.surface || seg.text || undefined}
              data-registry-only={seg.span.registry_only ? "1" : undefined}
              data-term-detail={seg.span.term_detail ? "1" : undefined}
              onMouseEnter={e => onHoverSpan && onHoverSpan(seg.span, block, e.currentTarget.getBoundingClientRect())}
              onMouseLeave={() => onLeaveSpan && onLeaveSpan()}
              onClick={e => {
                if (!onFocusSpan) return;
                e.stopPropagation();
                onFocusSpan(seg.span, e.currentTarget);
              }}>{seg.text}</mark>
          : <span key={i}>{seg.text}</span>;
      })}
    </>
  );
}

function TranslationCompare({
  translations, block, onHoverSpan, onLeaveSpan, activeHighlightId,
  focusedTermId, onFocusSpan,
}) {
  const entries = Object.entries(translations || {})
    .filter(([, row]) => row && (row.target_text || row.output_text))
    .sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) return null;
  return (
    <div className="translation-compare">
      <div className="tc-head">
        <Ic.eye size={12} />
        <span>{uiText("Các lần chạy dịch", "Translation runs")}</span>
        <span className="tc-sub">{uiText("chỉ đọc từ translation_runs", "read-only from translation_runs")}</span>
      </div>
      <div className="tc-grid">
        {entries.map(([key, row]) => (
          <div key={key} className="tc-card">
            <div className="tc-card-head">
              <span className="tc-label mono">{key}</span>
              <span className="tc-meta mono">{row.prompt_version || row.stage || ""}</span>
            </div>
            <div className="tc-text">
              <SpanText
                text={row.target_text || row.output_text || ""}
                spans={row.target_spans || []}
                block={block}
                onHoverSpan={onHoverSpan}
                onLeaveSpan={onLeaveSpan}
                activeHighlightId={activeHighlightId}
                focusedTermId={focusedTermId}
                onFocusSpan={onFocusSpan}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function formatInt(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n.toLocaleString("en-US") : "0";
}

function formatCost(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? `$${n.toFixed(n < 0.01 ? 5 : 3)}` : "$0";
}

function JsonBlock({ value, maxHeight = 220 }) {
  return (
    <pre className="obs-json" style={{ maxHeight }}>
      {typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2)}
    </pre>
  );
}

function PromptMessage({ title, message }) {
  return (
    <div className="obs-message">
      <div className="obs-message-head">
        <span className="mono">{title}</span>
        <span>{formatInt((message?.content || "").length)} {uiText("ký tự", "chars")}</span>
      </div>
      <pre>{message?.content || uiText("(trống)", "(empty)")}</pre>
    </div>
  );
}

function MemoryPackInspector({ detail }) {
  const pack = detail?.memory_pack;
  if (!detail) return <Empty icon={Ic.eye} text={uiText("Chọn một lượt gọi để kiểm tra prompt/ngữ cảnh.", "Select a call to inspect prompt/context.")} sub={uiText("APP-B01 chỉ đọc request_json và memory_packs đã cache.", "APP-B01 reads cached request_json and memory_packs only.")} />;
  if (detail.error) return <Empty icon={Ic.alert} text={uiText("Không tải được chi tiết lượt gọi.", "Call detail failed to load.")} sub={detail.error} />;
  if (!pack) {
    return (
      <div className="obs-card">
        <div className="obs-card-title"><Ic.layers size={13} />Memory pack</div>
        <p className="muted">{uiText("Lượt gọi này không có memory_pack liên kết. Đây là trạng thái bình thường với lượt gọi Builder/Judge và các hàng Translator không khớp cửa sổ translation_runs.", "No linked memory_pack for this call. This is expected for Builder/Judge calls and for Translator rows without a matching translation_runs window.")}</p>
      </div>
    );
  }
  const debug = pack.retrieval_debug || {};
  const audit = pack.context_audit || {};
  return (
    <div className="obs-card">
      <div className="obs-card-title"><Ic.layers size={13} />Memory pack</div>
      <div className="obs-kv-grid">
        <span><b>pack_id</b><em className="mono">{pack.pack_id}</em></span>
        <span><b>prompt_version</b><em className="mono">{pack.prompt_version || "-"}</em></span>
        <span><b>estimated_tokens</b><em className="mono">{formatInt(pack.estimated_tokens)}</em></span>
        <span><b>config</b><em className="mono">{pack.config || "-"}</em></span>
      </div>
      <div className="obs-pack-audit">
        <div>
          <span>{uiText("đã gồm", "included")}</span>
          <b>{formatInt(audit.included_count)}</b>
          <em>{uiText("đã gửi vào prompt", "sent to prompt")}</em>
        </div>
        <div>
          <span>{uiText("đã loại", "excluded")}</span>
          <b>{formatInt(audit.excluded_count)}</b>
          <em>{uiText("không liên quan / đã lọc", "not relevant / filtered")}</em>
        </div>
        <div>
          <span>{uiText("đã bỏ", "dropped")}</span>
          <b>{formatInt(audit.dropped_by_budget_count)}</b>
          <em>{uiText("do giới hạn ngân sách", "budget pressure")}</em>
        </div>
      </div>
      <div className="obs-audit-samples">
        <div>
          <div className="obs-subhead">{uiText("mẫu đã gồm", "included sample")}</div>
          <JsonBlock value={audit.included_sample || []} />
        </div>
        <div>
          <div className="obs-subhead">{uiText("mẫu đã loại", "excluded sample")}</div>
          <JsonBlock value={audit.excluded_sample || []} />
        </div>
        <div>
          <div className="obs-subhead">dropped_by_budget sample</div>
          <JsonBlock value={audit.dropped_by_budget_sample || []} />
        </div>
      </div>
      <div className="obs-subhead">anchors_count</div>
      <JsonBlock value={audit.anchors_count || {}} />
      <div className="obs-debug-grid">
        <div>
          <div className="obs-subhead">payload_json</div>
          <JsonBlock value={pack.payload} />
        </div>
        <div>
          <div className="obs-subhead">retrieval_debug_json</div>
          <JsonBlock value={debug} />
        </div>
      </div>
    </div>
  );
}

function RunEventSummary({ event }) {
  if (!event) {
    return <div className="muted">{uiText("Lần chạy này không có sự kiện sidecar. Lần chạy chỉ preflight hoặc phiên bản cũ có thể không phát sự kiện.", "No sidecar events for this run. Preflight-only and older runs may not emit events.")}</div>;
  }
  const usage = event.usage || {};
  const context = event.context_summary || {};
  const translations = event.translations || {};
  return (
    <div className="run-event-card">
      <div className="run-event-card-head">
        <span className="mono">{event.event}</span>
        <em className="mono">seq {event.seq || "-"} / {event.window_id || event.config || "-"}</em>
      </div>
      <div className="run-event-kv">
        <span><b>config</b><em className="mono">{event.config || "-"}</em></span>
        <span><b>prompt est</b><em className="mono">{formatInt(event.prompt_tokens_est)}</em></span>
        <span><b>cache</b><em className="mono">{event.from_cache === true ? "hit" : event.from_cache === false ? "miss" : "-"}</em></span>
        <span><b>cost</b><em className="mono">{formatCost(event.cost_usd)}</em></span>
      </div>
      {(usage.prompt_tokens || usage.completion_tokens || usage.cached_tokens) && (
        <div className="run-event-line mono">
          prompt {formatInt(usage.prompt_tokens)} / cached {formatInt(usage.cached_tokens)} / output {formatInt(usage.completion_tokens)}
        </div>
      )}
      {(context.included_count || context.dropped_by_budget_count || context.anchors_count) && (
        <div className="run-event-line mono">
          context included {formatInt(context.included_count)} / dropped {formatInt(context.dropped_by_budget_count)}
        </div>
      )}
      {Object.keys(translations).length > 0 && (
        <div className="run-event-preview">
          <div className="obs-subhead">{uiText("bản xem trước chưa commit", "uncommitted preview")}</div>
          {Object.entries(translations).slice(0, 3).map(([blockId, row]) => (
            <div key={blockId} className="run-event-preview-row">
              <span className="mono">{blockId}</span>
              <p>{row?.preview || ""}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function RunControlPanel({ runControl }) {
  if (!runControl) return null;
  const form = runControl.runForm || {};
  const preview = runControl.promptPreview || null;
  const runs = runControl.runs || [];
  const selectedLog = runControl.selectedRunLog || {};
  const selectedEvents = runControl.selectedRunEvents || { events: [] };
  const eventRows = selectedEvents.events || [];
  const latestEvent = eventRows[eventRows.length - 1] || null;
  const rep = preview?.representative_prompt || null;
  const previewMessages = rep?.messages || [];
  const previewSystem = previewMessages.find(m => m.role === "system");
  const previewUser = previewMessages.find(m => m.role === "user");
  const tokenEstimate = preview?.token_estimate || {};
  const selectedRun = runs.find(row => row.run_id === runControl.selectedRunId);

  return (
    <section className="obs-panel run-panel wb-pane">
      <div className="obs-panel-head wb-section-title">
        <span><Ic.play size={13} />{uiText("Điều khiển chạy", "Run Control")}</span>
        <em title={runControl.jobId || undefined}>{runControl.sourceTitle || uiText("không có project", "no project")}</em>
      </div>

      <div className="run-grid">
        <label>
          <span>{uiText("kịch bản", "script")}</span>
          <select value={form.script || "run_translate"} onChange={e => runControl.onFormChange({ script: e.target.value })}>
            {["run_translate", "run_prepass", "snapshot_runs", "score_run", "score_consistency", "build_memory", "build_index", "run_judge"].map(script => (
              <option key={script} value={script}>{script}</option>
            ))}
          </select>
        </label>
        <label>
          <span>{uiText("chương", "chapters")}</span>
          <input value={form.chapters || ""} onChange={e => runControl.onFormChange({ chapters: e.target.value })} placeholder="ch02 ch03" />
        </label>
        <label>
          <span>{uiText("cấu hình", "configs")}</span>
          <input value={form.configs || ""} onChange={e => runControl.onFormChange({ configs: e.target.value })} placeholder="S0 S1" />
        </label>
        <label>
          <span>{uiText("hồ sơ", "profile")}</span>
          <input value={form.profile || ""} onChange={e => runControl.onFormChange({ profile: e.target.value })} placeholder="literary_v1" />
        </label>
        <label>
          <span>{uiText("thí nghiệm", "experiment")}</span>
          <input value={form.experiment || ""} onChange={e => runControl.onFormChange({ experiment: e.target.value })} placeholder="translate_run" />
        </label>
        <label>
          <span>cache</span>
          <input value={form.cache || ""} onChange={e => runControl.onFormChange({ cache: e.target.value })} placeholder="data/jobs/translate_cache.sqlite3" />
        </label>
      </div>

      <div className="run-actions">
        <label className="run-check">
          <input type="checkbox" checked={!!form.allow_api} onChange={e => runControl.onFormChange({ allow_api: e.target.checked })} />
          <span>{uiText("cho phép API thật sau token xem trước", "allow real API after preview token")}</span>
        </label>
        <button className="btn sm" disabled={runControl.busy || !runControl.jobId || form.script !== "run_translate"} onClick={runControl.onPreview}>
          <Ic.eye size={13} />{uiText("Tạo bản xem trước prompt", "Render prompt preview")}
        </button>
        <button className="btn primary sm" disabled={runControl.busy || (!!form.allow_api && !preview?.confirm_token)} onClick={runControl.onCreateRun}>
          <Ic.play size={13} />{uiText("Khởi chạy", "Launch")}
        </button>
        <button className="btn sm" disabled={runControl.busy} onClick={runControl.onRefreshRuns}>
          <Ic.refresh size={13} />{uiText("Làm mới", "Refresh")}
        </button>
      </div>

      {runControl.error && (
        <div className="obs-gap"><Ic.alert size={13} /><span>{runControl.error}</span></div>
      )}

      {preview && (
        <div className="run-preview">
          <div className="run-preview-head">
            <span>{uiText("Đã cấp token xem trước prompt", "Prompt preview token issued")}</span>
            <em className="mono">{preview.confirm_token?.slice(0, 10)}... / {preview.planned_run_id || "no-run-id"} / {formatInt(rep?.prompt_tokens_est)} token prompt</em>
          </div>
          <div className="obs-breakdown">
            <div><span>{uiText("cửa sổ", "windows")}</span><b>{formatInt(tokenEstimate.configs?.S1?.windows || tokenEstimate.configs?.S0?.windows)}</b></div>
            <div><span>{uiText("prompt tối đa", "max prompt")}</span><b>{formatInt(tokenEstimate.configs?.S1?.prompt_tokens_max || tokenEstimate.configs?.S0?.prompt_tokens_max)}</b></div>
            <div><span>{uiText("tổng trần", "upper total")}</span><b>{formatInt(tokenEstimate.upper_total_all_configs)}</b></div>
            <div><span>{uiText("giới hạn ngày", "daily cap")}</span><b>{formatInt(tokenEstimate.daily_token_cap)}</b></div>
          </div>
          <div className="obs-debug-grid">
            <PromptMessage title="preview system" message={previewSystem} />
            <PromptMessage title="preview user" message={previewUser} />
          </div>
        </div>
      )}

      <div className="run-live-grid">
        <div className="run-list">
          <div className="obs-subhead">{uiText("registry lần chạy", "run registry")}</div>
          {runs.length ? runs.slice(0, 12).map(run => (
            <button key={run.run_id} className={"run-row" + (run.run_id === runControl.selectedRunId ? " on" : "")} onClick={() => runControl.onSelectRun(run.run_id)}>
              <span className={"run-status run-status-" + String(run.status || "").toLowerCase()}>{run.status}</span>
              <span className="mono">{run.script}</span>
              <em className="mono">{run.run_id}</em>
            </button>
          )) : (
            <div className="muted">{uiText("Chưa có hàng nào trong registry lần chạy.", "No run registry rows yet.")}</div>
          )}
        </div>
        <div className="run-log-box">
          <div className="obs-subhead">live log tail {selectedRun ? <span className="mono">· {selectedRun.run_id}</span> : null}</div>
          <pre>{selectedLog.log || uiText("Chọn hoặc khởi chạy một lần chạy để theo dõi stdout/stderr.", "Select or launch a run to tail stdout/stderr.")}</pre>
        </div>
      </div>
      <div className="run-events-box">
        <div className="obs-subhead">
          sidecar events
          {selectedRun ? <span className="mono"> / {selectedRun.run_id} / {formatInt(eventRows.length)} buffered</span> : null}
        </div>
        <div className="run-events-grid">
          <RunEventSummary event={latestEvent} />
          <div className="run-events-list">
            {eventRows.length ? eventRows.slice(-20).reverse().map((event, index) => (
              <div key={`${event.seq || index}:${event.event}`} className="run-event-row">
                <span className="mono">{event.seq || "-"}</span>
                <b>{event.event}</b>
                <em className="mono">{event.window_id || event.config || ""}</em>
              </div>
            )) : (
              <div className="muted">{uiText("Chưa theo dõi được sự kiện JSONL nào.", "No JSONL events tailed yet.")}</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

const AGENT_CONSOLE_STAGES = ["builder", "auditor", "translator", "cascade", "sf_qe", "sf_bt", "pj", "report"];

function RuntimeEmptyState({ runControl, view }) {
  const stats = runControl?.sourceStats || {};
  const chapterCount = Number(stats.chapters || 0);
  const blockCount = Number(stats.blocks || 0);
  const projectId = runControl?.sourceProjectId || "project";
  const title = runControl?.sourceTitle || projectId;
  const destination = view === "report" ? uiText("Báo cáo", "Report") : view === "observability" ? uiText("Quan sát", "Observability") : "Console";
  return (
    <section className="runtime-empty" aria-label={uiText(`${destination} chưa có lần chạy`, `${destination} has no run`)}>
      <div className="runtime-empty-icon"><Ic.play size={18} /></div>
      <div className="runtime-empty-kicker">{destination} · {uiText("chưa có lần chạy pipeline", "no pipeline run yet")}</div>
      <h2>{title}</h2>
      <p>
        {uiText("Dự án nguồn đã được nhập và chuẩn hóa thành", "The source project was imported and normalized into")} <b>{chapterCount} {uiText("chương", "chapters")}</b> · <b>{blockCount} block</b>,
        {uiText(" nhưng chưa được gắn với một cấu hình chạy Builder/Translator.", " but has not been attached to a Builder/Translator run configuration.")}
      </p>
      <div className="runtime-empty-flow" aria-label={uiText("Trạng thái chuẩn bị lần chạy", "Run preparation status")}>
        <span className="done"><Ic.checkCircle size={13} /> {uiText("Nguồn đã nhập", "Source imported")}</span>
        <span className="done"><Ic.checkCircle size={13} /> {uiText("Block đã trích", "Blocks extracted")}</span>
        <span><Ic.clock size={13} /> {uiText("Chưa cấu hình pipeline", "Pipeline not configured")}</span>
      </div>
      <p className="runtime-empty-note">
        {uiText("Khi một lần chạy được khởi tạo cho chính dự án này, Console sẽ hiện tiến trình và log; Báo cáo sẽ tổng hợp kết quả đã lưu và bằng chứng theo contract. Dự án vẫn là nguồn chuẩn; mỗi lần chạy chỉ dùng snapshot/index bất biến có hash, không tạo một dự án chỉnh sửa thứ hai.", "When a run is started for this project, Console will show progress and logs; Report will summarize persisted results and contract evidence. The project remains the source of truth; each run uses only an immutable hashed snapshot/index and does not create a second editable project.")}
      </p>
      <div className="runtime-empty-actions">
        {runControl?.onOpenProjectSource && (
          <button className="btn" type="button" onClick={runControl.onOpenProjectSource}>
            <Ic.folder size={13} /> {uiText("Mở Dự án / Nguồn", "Open Project / Source")}
          </button>
        )}
        {runControl?.onConfigurePipeline && (
          <button className="btn primary" type="button" onClick={runControl.onConfigurePipeline}>
            <Ic.play size={13} /> {uiText("Cấu hình pipeline", "Configure pipeline")}
          </button>
        )}
      </div>
      <div className="runtime-empty-id mono">project_id: {projectId}</div>
    </section>
  );
}

function AgentConsole({ runControl, onBack, onOpenReport }) {
  // Thin adapter: maps runControl -> AgentConsoleView (console.jsx, skin ported from Claude Design).
  const [consoleTheme, setConsoleTheme] = React.useState(() => (localStorage.getItem("ailab.console_theme") === "dark" ? "dark" : "paper"));
  function toggleConsoleTheme() {
    setConsoleTheme(prev => { const next = prev === "paper" ? "dark" : "paper"; localStorage.setItem("ailab.console_theme", next); return next; });
  }
  if (!runControl) return null;
  if (!runControl.runtimeAvailable) {
    return (
      <div className={"console-route-empty agentconsole console-theme-" + consoleTheme}>
        <div className="console-route-empty-head">
          <button className="btn console-back" type="button" onClick={onBack}>&larr; Workspace</button>
          <span className="brand">⬢ AGENT CONSOLE</span>
          <nav className="run-surface-tabs" aria-label={uiText("Các chế độ lần chạy", "Run views")}>
            <span className="run-surface-tab active" aria-current="page">Console</span>
            {onOpenReport && <button className="run-surface-tab" type="button" onClick={onOpenReport}>{uiText("Báo cáo", "Report")}</button>}
          </nav>
          <span className="mono">{runControl.sourceProjectId || "no project"}</span>
        </div>
        <RuntimeEmptyState runControl={runControl} view="console" />
      </div>
    );
  }
  const sel = runControl.selectedRunEvents || {};
  const rawLog = String(runControl.selectedRunLog?.log || "").trim();
  const selectedRun = (runControl.runs || []).find(r => r.run_id === runControl.selectedRunId) || null;
  const ConsoleView = typeof window !== "undefined" ? window.AgentConsoleView : null;
  if (!ConsoleView) {
    return (
      <div className="agentconsole">
        <div className="console-shell">
          <div className="artifact-path">{uiText("Đang tải Agent Console...", "Agent Console loading...")}</div>
        </div>
      </div>
    );
  }
  return (
    <div className="runtime-console-wrap">
      <ConsoleView
        runId={runControl.selectedRunId}
        runs={runControl.runs || []}
        onSelectRun={runControl.onSelectRun}
        events={sel.events || []}
        running={!!sel.running}
        status={(selectedRun && selectedRun.status) || sel.status || ""}
        truncated={!!sel.truncated}
        partialLine={!!sel.partial_line}
        blockPreview={runControl.blockPreview || sel.blockPreview || []}
        watchlist={runControl.watchlist || sel.watchlist || []}
        reportSummary={runControl.reportSummary || sel.reportSummary || null}
        workflowReplay={runControl.workflowReplay || sel.workflowReplay || null}
        projectId={runControl.sourceProjectId || ""}
        onBack={onBack}
        onOpenReport={onOpenReport}
        theme={consoleTheme}
        onToggleTheme={toggleConsoleTheme}
        onRefresh={runControl.onRefreshRuns}
        busy={runControl.busy}
        onPause={runControl.onPause}
        onCancel={runControl.onCancel}
        onResume={runControl.onResume}
        onDich={runControl.onDich}
      />
      {rawLog && (
        <details className="runtime-raw-log" open={!(sel.events || []).length}>
          <summary>{uiText("Log preflight / tiến trình thô", "Raw preflight / process log")}</summary>
          <pre>{rawLog.slice(-24000)}</pre>
        </details>
      )}
    </div>
  );
}

function AgentReport({ runControl, onBack, onOpenConsole }) {
  const [reportTheme, setReportTheme] = React.useState(() => (
    localStorage.getItem("ailab.console_theme") === "dark" ? "dark" : "light"
  ));
  function toggleReportTheme() {
    setReportTheme(previous => {
      const next = previous === "dark" ? "light" : "dark";
      localStorage.setItem("ailab.console_theme", next === "dark" ? "dark" : "paper");
      return next;
    });
  }
  if (!runControl) return null;
  const selectedRun = (runControl.runs || []).find(run => run.run_id === runControl.selectedRunId) || null;
  const ReportView = typeof window !== "undefined" ? window.AgentReportView : null;
  if (!ReportView) {
    return (
      <div className="agentreport report-theme-light">
        <div className="report-loading">{uiText("Đang tải Báo cáo lần chạy...", "Run Report loading...")}</div>
      </div>
    );
  }
  return (
    <ReportView
      runId={runControl.selectedRunId || ""}
      runs={runControl.runs || []}
      selectedRun={selectedRun}
      onSelectRun={runControl.onSelectRun}
      report={runControl.reportSummary || null}
      reportSource="report-summary"
      runtimeAvailable={!!runControl.runtimeAvailable}
      projectId={runControl.sourceProjectId || ""}
      projectTitle={runControl.sourceTitle || ""}
      onBack={onBack}
      onOpenConsole={onOpenConsole}
      onRefresh={runControl.onRefreshRuns}
      theme={reportTheme}
      onToggleTheme={toggleReportTheme}
    />
  );
}

function normalizeConsoleEvent(row) {
  const payload = row?.payload && typeof row.payload === "object" ? row.payload : row || {};
  return {
    ...row,
    payload,
    event_type: row?.event_type || row?.event || "event",
    stage: row?.stage || payload.stage || "translator",
    agent: row?.agent || payload.agent || "Agent",
    severity: row?.severity || (row?.event === "error" ? "error" : row?.event === "warning" ? "warning" : "info"),
  };
}

function uniqueSorted(values) {
  return [...new Set(values)].sort((a, b) => String(a).localeCompare(String(b)));
}

function ageSeconds(ts) {
  const value = Date.parse(ts || "");
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.floor((Date.now() - value) / 1000));
}

function LegacyObservabilityView({ observability, runControl, selectedCallId, selectedCallDetail, callDetailLoading, onSelectCall }) {
  if (!runControl?.runtimeAvailable) return <RuntimeEmptyState runControl={runControl} view="observability" />;
  const calls = observability?.calls || [];
  const totals = observability?.totals?.overall || {};
  const detail = selectedCallDetail || calls.find(call => call.call_id === selectedCallId) || calls[0] || null;
  const messages = detail?.messages || [];
  const systemMessage = messages.find(m => m.role === "system") || null;
  const userMessage = messages.find(m => m.role === "user") || null;
  const breakdown = detail?.token_breakdown || {};

  return (
    <div className="obs-view wb-operational">
      <div className="obs-head wb-toolbar">
        <div>
          <div className="obs-kicker">ObservabilityReadModel</div>
          <h2>{uiText("Quan sát Prompt / Ngữ cảnh / Cache", "Prompt / Context / Cache Observability")}</h2>
        </div>
        <div className="obs-source mono">{observability?.meta?.job_id || "no job"}</div>
      </div>

      <RunControlPanel runControl={runControl} />

      {observability?.meta?.known_gap && (
        <div className="obs-gap">
          <Ic.alert size={13} />
          <span>{observability.meta.known_gap}</span>
        </div>
      )}

      <div className="obs-metrics">
        <div><span>{uiText("lượt gọi", "calls")}</span><b>{formatInt(totals.calls)}</b></div>
        <div><span>{uiText("token quota", "quota tokens")}</span><b>{formatInt(totals.total_quota_tokens)}</b></div>
        <div><span>{uiText("đầu vào cache", "cached input")}</span><b>{formatInt(totals.cached_tokens)}</b></div>
        <div><span>{uiText("hàng chi phí", "cost rows")}</span><b>{formatCost(totals.cost_usd)}</b></div>
      </div>

      <div className="obs-grid wb-split">
        <section className="obs-panel obs-call-list wb-pane">
          <div className="obs-panel-head wb-section-title">
            <span><Ic.list size={13} />{uiText("Lượt gọi API", "API calls")}</span>
            <em>{formatInt(calls.length)} {uiText("hàng kết quả cache", "cached result rows")}</em>
          </div>
          <div className="obs-table">
            {calls.map(call => (
              <button
                key={call.call_id}
                aria-selected={call.call_id === selectedCallId}
                className={"obs-row wb-record-row" + (call.call_id === selectedCallId ? " on" : "")}
                onClick={() => onSelectCall(call.call_id)}
              >
                <span className={"obs-agent obs-agent-" + String(call.agent || "LLM").toLowerCase()}>{call.agent}</span>
                <span className="obs-tag mono">{call.tag}</span>
                <span className="mono">{formatInt(call.usage?.prompt_tokens)}</span>
                <span className="mono">{formatInt(call.usage?.cached_tokens)}</span>
                <span className="mono">{formatInt(call.usage?.completion_tokens)}</span>
                <span className="mono">{formatCost(call.cost_usd)}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="obs-panel obs-detail wb-pane wb-detail">
          <div className="obs-panel-head wb-section-title">
            <span><Ic.eye size={13} />{uiText("Trình kiểm tra Prompt / Ngữ cảnh", "Prompt / Context Inspector")}</span>
            {callDetailLoading ? <em>{uiText("đang tải...", "loading...")}</em> : <em className="mono">{detail?.call_id || uiText("không có lượt gọi", "no call")}</em>}
          </div>

          {detail ? (
            <>
              <div className="obs-detail-meta">
                <span><b>agent</b>{detail.agent}</span>
                <span><b>tag</b><em className="mono">{detail.tag}</em></span>
                <span><b>model</b><em className="mono">{detail.model}</em></span>
                <span><b>prompt_version</b><em className="mono">{detail.prompt_version || detail.memory_pack?.prompt_version || "-"}</em></span>
                <span><b>provider cached</b>{formatInt(detail.usage?.cached_tokens)}</span>
                <span><b>quota tokens</b>{formatInt(detail.usage?.total_quota_tokens)}</span>
              </div>

              <div className="obs-breakdown">
                <div><span>prompt</span><b>{formatInt(detail.usage?.prompt_tokens)}</b></div>
                <div><span>completion</span><b>{formatInt(detail.usage?.completion_tokens)}</b></div>
                <div><span>memory pack est.</span><b>{formatInt(breakdown.estimated_memory_pack_tokens)}</b></div>
                <div><span>other prompt est.</span><b>{formatInt(breakdown.estimated_other_prompt_tokens)}</b></div>
              </div>

              <div className="obs-debug-grid">
                <PromptMessage title="system" message={systemMessage} />
                <PromptMessage title="user" message={userMessage} />
              </div>

              <MemoryPackInspector detail={detail} />

              <div className="obs-card wb-section">
                <div className="obs-card-title"><Ic.clock size={13} />{uiText("Ngữ nghĩa cache / chi phí", "Cache / cost semantics")}</div>
                <div className="obs-kv-grid">
                  <span><b>local replay row</b><em>{detail.cache?.local_replay?.stored_result ? "stored" : "n/a"}</em></span>
                  <span><b>replay hit events</b><em>{detail.cache?.local_replay?.hit_events_logged ? "logged" : "not logged"}</em></span>
                  <span><b>provider cached</b><em>{formatInt(detail.cache?.provider?.cached_tokens)}</em></span>
                  <span><b>quota rule</b><em>input + output</em></span>
                </div>
              </div>
            </>
          ) : (
            <Empty icon={Ic.eye} text={uiText("Job này không có hàng quan sát nào.", "No observability rows for this job.")} sub={uiText("Có thể đọc pipeline DB nhưng không có hàng cache nào khớp job này.", "The pipeline DB is readable, but no cache rows matched this job.")} />
          )}
        </section>
      </div>
    </div>
  );
}

function compactList(value, empty = "none") {
  if (Array.isArray(value)) return value.length ? value.join(", ") : empty;
  if (typeof value === "object" && value !== null) {
    const rows = Object.entries(value).filter(([, v]) => v !== undefined && v !== null && v !== "");
    return rows.length ? rows.map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join("; ") : empty;
  }
  if (value === undefined || value === null || value === "") return empty;
  return String(value);
}

function limitItems(items, limit = 4) {
  const rows = (items || []).filter(Boolean);
  if (rows.length <= limit) return rows.join(", ");
  return rows.slice(0, limit).join(", ") + ` +${rows.length - limit}`;
}

function confidenceText(value) {
  if (value === undefined || value === null || value === "") return "n/a";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : String(value);
}

function statusLabel(span, item) {
  return span?.display_status || span?.status || item?.overlay_status || item?.status || "unscored";
}

function statusDisplayLabel(span, item) {
  const status = statusLabel(span, item);
  if (status === "localization_mismatch") return uiText("Lệch bản dịch chuẩn", "Canonical translation mismatch");
  if (status === "localization_source_warning") return uiText("Có bản VI lệch chuẩn", "Has a mismatched VI version");
  if (status === "localized") return span?.target ? uiText("Khớp bản dịch chuẩn", "Matches canonical translation") : uiText("Đã định vị", "Located");
  if (status === "localized_only") return uiText("Đã định vị", "Located");
  return status;
}

function isLocalizationSpan(span) {
  const source = String(span?.mark_source || "");
  return source === "localization_source" || source.startsWith("cascade_");
}

function runtimeFormsLabel(span, item) {
  if (span?.forms_used && Object.keys(span.forms_used).length) return compactList(span.forms_used);
  const byConfig = item?.overlay_status_by_config || {};
  return Object.keys(byConfig).length ? compactList(byConfig) : "unscored";
}

function locatedByLabel(span) {
  const value = span?.located_by || "";
  if (value === "code_exact") return "code exact";
  if (value === "ai_locate_local") return "AI locate (Gemma)";
  if (value === "ai_locate_fallback") return "AI locate (GPT fallback)";
  if (value === "block_detect") return "block-level detect";
  return compactList(value);
}

function markFlagLabel(span) {
  const flags = [];
  if (span?.masquerade_suspect) flags.push("masquerade_suspect");
  if (span?.clean_text_fallback) flags.push("clean_text_fallback");
  if (span?.gpt_fallback) flags.push("gpt_fallback");
  if (span?.cross_term_overlap) flags.push("cross_term_overlap");
  return flags.join(", ");
}

function hoverCardPosition(rect) {
  if (!rect) return { top: 0, left: 0, above: false };
  const width = 328;
  const left = Math.min(Math.max(12, rect.left), Math.max(12, window.innerWidth - width - 12));
  const above = rect.bottom > window.innerHeight - 250;
  return {
    left,
    top: above ? rect.top - 8 : rect.bottom + 8,
    above,
  };
}

function HighlightHoverCard({ hover, linkIndex }) {
  if (!hover?.span || !hover?.rect) return null;
  const { span, block } = hover;
  const pos = hoverCardPosition(hover.rect);

  if (span.kind === "entity") {
    const data = linkIndex?.entities?.[span.id];
    const entity = data?.item;
    if (!entity) return null;
    const currentMentions = data.mentionsByBlock?.[block.block_id] || [];
    const chapterLabels = (data.chapters || []).map(ch => ch.title || ch.chapter_id);
    const summaryLabels = (data.summaryChapters || []).map(ch => ch.title || ch.chapter_id);
    const runtime = entity.provenance?.branch === "runtime_memory" || span.provenance;
    return (
      <div className={"hl-card" + (pos.above ? " above" : "")} style={{ top: pos.top, left: pos.left }}>
        <div className="hl-card-head">
          <span className="hl-card-kind entity"><Ic.users size={12} />{runtime ? uiText("Thực thể runtime", "Runtime entity") : uiText("Thực thể", "Entity")}</span>
          <span className={"hl-card-status status-" + statusLabel(span, entity)}>{statusDisplayLabel(span, entity)}</span>
        </div>
        <div className="hl-card-title">
          <span>{entity.canonical_source || span.id}</span>
          <span className="hl-card-arrow">-</span>
          <span>{entity.canonical_target || uiText("cần bản đích", "target needed")}</span>
        </div>
        <div className="hl-card-grid">
          <span>{uiText("loại", "type")}</span><b>{compactList(entity.entity_type || entity.type)}</b>
          {isLocalizationSpan(span) ? (
            <>
              <span>{uiText("nguồn", "source")}</span><b>Localization</b>
              {span.located_by && <><span>{uiText("định vị bởi", "located by")}</span><b>{locatedByLabel(span)}</b></>}
              <span>{uiText("bề mặt", "surface")}</span><b>{compactList(span.surface || span.matched_form)}</b>
            </>
          ) : runtime ? (
            <>
              <span>forms_used</span><b>{runtimeFormsLabel(span, entity)}</b>
              {span.located_by && <><span>{uiText("định vị bởi", "located by")}</span><b>{locatedByLabel(span)}</b></>}
              {markFlagLabel(span) && <><span>{uiText("cờ", "flags")}</span><b>{markFlagLabel(span)}</b></>}
              <span>{uiText("nguồn gốc", "provenance")}</span><b>{entity.provenance?.label || span.provenance || "agent-built"}</b>
              <span>{uiText("bề mặt", "surface")}</span><b>{compactList(span.surface || span.matched_form)}</b>
            </>
          ) : (
            <>
              <span>{uiText("giới", "gender")}</span><b>{compactList(entity.gender)}</b>
              <span>{uiText("bí danh", "aliases")}</span><b>{compactList(entity.aliases_target)}</b>
              <span>{uiText("đại từ", "pronoun")}</span><b>{compactList(entity.pronoun_policy)}</b>
              <span>{uiText("người chú giải", "annotator")}</span><b>{compactList(entity.annotated_by)}</b>
            </>
          )}
        </div>
        {span.stale && <div className="hl-card-warning"><Ic.alert size={12} />{uiText("Span này cần gắn thẻ lại.", "This span needs re-tag.")}</div>}
        <div className="hl-card-section">
          <div className="hl-card-section-title">{uiText("Liên kết", "Links")}</div>
          <div className="hl-card-links">
            <span>{uiText(`${currentMentions.length} lần nhắc trong block này`, `${currentMentions.length} mention${currentMentions.length === 1 ? "" : "s"} in this block`)}</span>
            <span>{uiText(`${data.mentions.length} tổng lần nhắc`, `${data.mentions.length} total mention${data.mentions.length === 1 ? "" : "s"}`)}</span>
            <span>{data.blockIds.length} block</span>
            <span>{data.chapters.length} {uiText("chương", `chapter${data.chapters.length === 1 ? "" : "s"}`)}</span>
            <span>{data.speakerBlocks.length} {uiText("block người nói", `speaker block${data.speakerBlocks.length === 1 ? "" : "s"}`)}</span>
            <span>{data.addresseeBlocks.length} {uiText("block người nghe", `addressee block${data.addresseeBlocks.length === 1 ? "" : "s"}`)}</span>
          </div>
          {chapterLabels.length > 0 && <div className="hl-card-small"><b>{uiText("chương", "chapters")}</b> {limitItems(chapterLabels)}</div>}
          {summaryLabels.length > 0 && <div className="hl-card-small"><b>characters_present</b> {limitItems(summaryLabels)}</div>}
        </div>
      </div>
    );
  }

  if (span.kind === "glossary") {
    const data = linkIndex?.glossary?.[span.id];
    const term = data?.item || span.term_detail;
    if (!term) return null;
    const detailOccurrences = span.detail_occurrences || [];
    const occurrences = data?.occurrences || detailOccurrences;
    const currentOccurrences = data?.occurrencesByBlock?.[block.block_id]
      || detailOccurrences.filter(item => item.block_id === block.block_id);
    const blockIds = data?.blockIds
      || [...new Set(detailOccurrences.map(item => item.block_id).filter(Boolean))];
    const chapters = data?.chapters || [...new Set(blockIds
      .map(blockId => linkIndex?.blockById?.[blockId]?.chapter_id)
      .filter(Boolean))]
      .map(chapterId => ({
        chapter_id: chapterId,
        title: linkIndex?.chapterById?.[chapterId]?.title || chapterId,
      }));
    const chapterLabels = chapters.map(ch => ch.title || ch.chapter_id);
    const registryOnly = !!span.registry_only && !data;
    const runtime = !registryOnly && (term.provenance?.branch === "runtime_memory" || span.provenance);
    const localization = isLocalizationSpan(span);
    const totalOccurrences = registryOnly
      ? Number(term.occurrences_count || occurrences.length)
      : occurrences.length;
    return (
      <div className={"hl-card" + (pos.above ? " above" : "")} style={{ top: pos.top, left: pos.left }}>
        <div className="hl-card-head">
          <span className="hl-card-kind glossary"><Ic.tag size={12} />{localization ? uiText("Thuật ngữ bản địa hóa", "Localization term") : registryOnly ? uiText("Thuật ngữ registry", "Registry term") : runtime ? uiText("Thuật ngữ runtime", "Runtime term") : uiText("Thuật ngữ", "Glossary")}</span>
          <span className={"hl-card-status status-" + statusLabel(span, term)}>{statusDisplayLabel(span, term)}</span>
        </div>
        <div className="hl-card-title">
          <span>{term.source_term || span.id}</span>
          <span className="hl-card-arrow">-</span>
          <span>{term.expected_target || uiText("cần bản đích", "target needed")}</span>
        </div>
        <div className="hl-card-grid">
          {isLocalizationSpan(span) ? (
            <>
              <span>{uiText("nguồn", "source")}</span><b>Localization</b>
              {span.located_by && <><span>{uiText("định vị bởi", "located by")}</span><b>{locatedByLabel(span)}</b></>}
              <span>{uiText("bề mặt", "surface")}</span><b>{compactList(span.surface || span.matched_form)}</b>
              {span.accepted_forms?.length > 0 && <><span>{uiText("bản dịch chuẩn", "canonical translation")}</span><b>{compactList(span.accepted_forms)}</b></>}
              {span.mismatch_configs?.length > 0 && <><span>{uiText("bản lệch", "mismatched versions")}</span><b>{compactList(span.mismatch_configs)}</b></>}
              {markFlagLabel(span) && <><span>{uiText("cờ kiểm tra", "review flags")}</span><b>{markFlagLabel(span)}</b></>}
            </>
          ) : runtime ? (
            <>
              <span>forms_used</span><b>{runtimeFormsLabel(span, term)}</b>
              {span.located_by && <><span>{uiText("định vị bởi", "located by")}</span><b>{locatedByLabel(span)}</b></>}
              {markFlagLabel(span) && <><span>{uiText("cờ", "flags")}</span><b>{markFlagLabel(span)}</b></>}
              <span>tier</span><b>{compactList(span.constraint_strength || term.constraint_strength)}</b>
              <span>{uiText("phạm vi", "scope")}</span><b>{compactList(term.chapter_scope)}</b>
              <span>{uiText("nguồn gốc", "provenance")}</span><b>{term.provenance?.label || span.provenance || "agent-built"}</b>
              <span>{uiText("bề mặt", "surface")}</span><b>{compactList(span.surface || span.matched_form)}</b>
              {span.target && (
                <>
                  <span>{uiText("khớp", "match")}</span><b>{uiText("đã phát hiện bề mặt đích trong block này; đây là khớp bề mặt, không phải alignment", "detected target surface in this block; surface match, not alignment")}</b>
                </>
              )}
            </>
          ) : (
            <>
              <span>{uiText("được phép", "allowed")}</span><b>{compactList(term.allowed_variants)}</b>
              <span>{uiText("bị cấm", "forbidden")}</span><b>{compactList(term.forbidden_variants)}</b>
              <span>{uiText("lĩnh vực", "domain")}</span><b>{compactList(term.domain)}</b>
              <span>{uiText("phạm vi", "scope")}</span><b>{compactList(term.chapter_scope)}</b>
              <span>{uiText("người chú giải", "annotator")}</span><b>{compactList(term.annotated_by)}</b>
              <span>{uiText("độ tin cậy", "confidence")}</span><b>{confidenceText(term.confidence)}</b>
            </>
          )}
        </div>
        {span.stale && <div className="hl-card-warning"><Ic.alert size={12} />{uiText("Span này cần gắn thẻ lại.", "This span needs re-tag.")}</div>}
        {registryOnly && <div className="hl-card-warning"><Ic.alert size={12} />{uiText("Bản ghi registry chỉ đọc; không nằm trong ngữ cảnh lần chạy này.", "Read-only registry record; not included in this run context.")}</div>}
        <div className="hl-card-section">
          <div className="hl-card-section-title">{uiText("Liên kết", "Links")}</div>
          <div className="hl-card-links">
            <span>{uiText(`${currentOccurrences.length} lần xuất hiện trong block này`, `${currentOccurrences.length} occurrence${currentOccurrences.length === 1 ? "" : "s"} in this block`)}</span>
            <span>{uiText(`${totalOccurrences} lần xuất hiện đã lưu`, `${totalOccurrences} stored occurrence${totalOccurrences === 1 ? "" : "s"}`)}</span>
            <span>{uiText(`${blockIds.length} block trong phạm vi đã tải`, `${blockIds.length} block${blockIds.length === 1 ? "" : "s"} in loaded scope`)}</span>
            <span>{uiText(`${chapters.length} chương trong phạm vi đã tải`, `${chapters.length} chapter${chapters.length === 1 ? "" : "s"} in loaded scope`)}</span>
          </div>
          {chapterLabels.length > 0 && <div className="hl-card-small"><b>chapters</b> {limitItems(chapterLabels)}</div>}
        </div>
      </div>
    );
  }

  return null;
}

function CleanTextSurface({
  block, spans = [], editing, draft, onDraft, onMouseUp, cleanRef, taRef, onAddGlossary, onAddEntity, selection,
  onHoverSpan, onLeaveSpan, readOnly, activeHighlightId, focusedTermId, onFocusSpan
}) {
  if (editing) {
    return (
      <textarea className="clean-edit mono" ref={taRef} value={draft}
        onChange={e => onDraft(e.target.value)} spellCheck={false} />
    );
  }
  return (
    <div
      className={"clean-text" + (block.block_type === "prose" ? " clean-text-flow" : "")}
      ref={cleanRef}
      onMouseUp={onMouseUp}
    >
      <SpanText
        text={block.clean_text || ""}
        spans={spans}
        block={block}
        onHoverSpan={onHoverSpan}
        onLeaveSpan={onLeaveSpan}
        activeHighlightId={activeHighlightId}
        focusedTermId={focusedTermId}
        onFocusSpan={onFocusSpan}
      />
      {!readOnly && <SelectionPopover rect={selection?.rect}
        onGlossary={onAddGlossary}
        onEntity={onAddEntity} />}
    </div>
  );
}

function ChapterBlockRow({
  block, spans, reviewed, active, onSelectBlock, onCommitClean,
  onMarkReviewed, onAddGlossary, onAddEntity, onHoverSpan, onLeaveSpan, readOnly, activeHighlightId,
  focusedTermId, onFocusSpan
}) {
  const cleanRef = React.useRef(null);
  const taRef = React.useRef(null);
  const [sel, setSel] = React.useState(null);
  const [editing, setEditing] = React.useState(false);
  const [sourceOpen, setSourceOpen] = React.useState(false);
  const [draft, setDraft] = React.useState(block.clean_text || "");

  React.useEffect(() => { setDraft(block.clean_text || ""); }, [block.block_id, block.clean_text]);
  React.useEffect(() => { if (editing && taRef.current) taRef.current.focus(); }, [editing]);

  function clearSelection() {
    setSel(null);
    const current = window.getSelection();
    if (current) current.removeAllRanges();
  }

  function handleMouseUp() {
    if (editing || readOnly) return;
    const c = cleanRef.current;
    if (!c) return;
    const off = selectionOffsets(c);
    if (!off) { setSel(null); return; }
    onSelectBlock(block.block_id);
    if (onLeaveSpan) onLeaveSpan();
    const r = window.getSelection().getRangeAt(0).getBoundingClientRect();
    const host = c.getBoundingClientRect();
    const popoverWidth = 258;
    const top = r.bottom - host.top + 8;
    const left = Math.min(
      Math.max(8, r.left - host.left + r.width / 2 - popoverWidth / 2),
      Math.max(8, c.clientWidth - popoverWidth - 8)
    );
    setSel({ ...off, rect: { top, left } });
  }

  const staleCount = (spans || []).filter(s => s.stale).length;
  const flags = (block.quality_flags || []).filter(f => f !== "ok");

  return (
    <article
      className={"chapter-block-row" + (active ? " active" : "") + (reviewed ? " reviewed" : "")}
      data-block-id={block.block_id}
      onMouseDown={() => onSelectBlock(block.block_id)}
    >
      <div className="cbr-head">
        <div className="cbr-meta">
          <span className="mono cbr-id">{block.block_id}</span>
          <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
          {reviewed && <span className="mini-badge good"><Ic.check size={10} />{uiText("đã duyệt", "reviewed")}</span>}
          {flags.map(f => <span key={f} className="mini-badge bad"><Ic.flag size={10} />{f}</span>)}
          {staleCount > 0 && (
            <span className="stale-warn tip" data-tip={uiText("Một span thuật ngữ/thực thể không còn khớp nội dung block. Hãy gắn thẻ lại hàng này.", "A glossary/entity span no longer matches this block text. Re-tag this row.")}>
              <Ic.alert size={11} />{uiText(`${staleCount} span cần gắn lại`, `${staleCount} span${staleCount > 1 ? "s" : ""} need re-tag`)}
            </span>
          )}
        </div>
        <div className="cbr-actions">
          <button className="btn sm ghost" onClick={e => { e.stopPropagation(); setSourceOpen(v => !v); }}>
            <Ic.eye size={12} />{sourceOpen ? uiText("Ẩn nguồn", "Hide source") : uiText("Nguồn", "Source")}
          </button>
          {!readOnly && !editing ? (
            <button className="btn sm" onClick={e => { e.stopPropagation(); clearSelection(); setEditing(true); }}>
              <Ic.pencil size={11} />{uiText("Sửa", "Edit")}
            </button>
          ) : !readOnly && (
            <>
              <button className="btn sm" onClick={e => { e.stopPropagation(); setDraft(block.clean_text || ""); setEditing(false); }}>{uiText("Hủy", "Cancel")}</button>
              <button className="btn sm primary" onClick={e => { e.stopPropagation(); setEditing(false); onCommitClean(block.block_id, draft); }}>
                <Ic.check size={11} />{uiText("Lưu", "Save")}
              </button>
            </>
          )}
          {!readOnly && <button className={"btn sm reviewed-btn" + (reviewed ? " is-on" : "")}
            onClick={e => { e.stopPropagation(); onMarkReviewed(block.block_id); }}>
            <Ic.checkCircle size={13} />{reviewed ? uiText("Đã duyệt", "Reviewed") : uiText("Duyệt", "Review")}
          </button>}
        </div>
      </div>

      {sourceOpen && (
        <div className="chapter-source">
          <div className="field-head compact">
            <span className="fh-title"><Ic.lock size={11} />{uiText("Nguồn (EN)", "Source (EN)")}</span>
            <span className="fh-meta">{uiText("chỉ đọc", "read-only")} · source_text</span>
          </div>
          <div className="src-text compact">{block.source_text || ""}</div>
        </div>
      )}

      <CleanTextSurface
        block={block}
        spans={spans}
        editing={editing}
        draft={draft}
        onDraft={setDraft}
        cleanRef={cleanRef}
        taRef={taRef}
        selection={sel}
        onMouseUp={handleMouseUp}
        onHoverSpan={onHoverSpan}
        onLeaveSpan={onLeaveSpan}
        activeHighlightId={activeHighlightId}
        focusedTermId={focusedTermId}
        onFocusSpan={onFocusSpan}
        readOnly={readOnly}
        onAddGlossary={() => { onAddGlossary(block.block_id, sel); clearSelection(); }}
        onAddEntity={() => { onAddEntity(block.block_id, sel); clearSelection(); }}
      />
      <TranslationCompare
        translations={block.translations}
        block={block}
        onHoverSpan={onHoverSpan}
        onLeaveSpan={onLeaveSpan}
        activeHighlightId={activeHighlightId}
        focusedTermId={focusedTermId}
        onFocusSpan={onFocusSpan}
      />
    </article>
  );
}

function SingleBlockView({
  block, docInfo, reviewed, spans, editing, onEdit, onCommitClean, onCancelEdit,
  onAddGlossary, onAddEntity, onHoverSpan, onLeaveSpan, readOnly, activeHighlightId,
  focusedTermId, onFocusSpan
}) {
  const cleanRef = React.useRef(null);
  const taRef = React.useRef(null);
  const [sel, setSel] = React.useState(null);
  const [draft, setDraft] = React.useState(block.clean_text || "");

  React.useEffect(() => { setDraft(block.clean_text || ""); }, [block.block_id, block.clean_text]);
  React.useEffect(() => { if (editing && taRef.current) { taRef.current.focus(); } }, [editing]);

  function clearSelection() {
    setSel(null);
    const current = window.getSelection();
    if (current) current.removeAllRanges();
  }

  function handleMouseUp() {
    if (editing || readOnly) return;
    const c = cleanRef.current;
    if (!c) return;
    const off = selectionOffsets(c);
    if (!off) { setSel(null); return; }
    if (onLeaveSpan) onLeaveSpan();
    const r = window.getSelection().getRangeAt(0).getBoundingClientRect();
    const host = c.getBoundingClientRect();
    const popoverWidth = 258;
    const top = r.bottom - host.top + 8;
    const left = Math.min(
      Math.max(8, r.left - host.left + r.width / 2 - popoverWidth / 2),
      Math.max(8, c.clientWidth - popoverWidth - 8)
    );
    setSel({ ...off, rect: { top, left } });
  }

  const staleCount = (spans || []).filter(s => s.stale).length;

  return (
    <>
      <div className="ed-scroll" onMouseDown={() => sel && setSel(null)}>
        <div className="ed-inner">
          <div className="field-block">
            <div className="field-head">
              <span className="fh-title"><Ic.lock size={11} />{uiText("Nguồn (EN)", "Source (EN)")}</span>
              <span className="fh-meta">{uiText("chỉ đọc", "read-only")} · source_text · {uiText("đã trích", "extracted")}</span>
            </div>
            <div className="src-text">{block.source_text || ""}</div>
          </div>

          <div className="field-block">
            <div className="field-head">
              <span className="fh-title editable-title"><Ic.pencil size={11} />{uiText("Văn bản sạch", "Clean text")}</span>
              <span className="fh-actions">
                {staleCount > 0 && !editing && (
                  <span className="stale-warn tip" data-tip={uiText("Một span thuật ngữ/thực thể không còn khớp văn bản đã sửa. Hãy gắn thẻ lại từ panel bên phải.", "A glossary/entity span no longer matches the edited text. Re-tag from the right panel.")}>
                    <Ic.alert size={11} />{uiText(`${staleCount} span cần gắn lại`, `${staleCount} span${staleCount > 1 ? "s" : ""} need re-tag`)}
                  </span>
                )}
                {!readOnly && !editing
                  ? <button className="btn sm" onClick={() => { setSel(null); onEdit(); }}><Ic.pencil size={11} />{uiText("Sửa", "Edit")}</button>
                  : !readOnly && <>
                      <button className="btn sm" onClick={() => { setDraft(block.clean_text || ""); onCancelEdit(); }}>{uiText("Hủy", "Cancel")}</button>
                      <button className="btn sm primary" onClick={() => onCommitClean(block.block_id, draft)}><Ic.check size={11} />{uiText("Lưu văn bản", "Save text")}</button>
                    </>}
              </span>
            </div>

            <CleanTextSurface
              block={block}
              spans={spans}
              editing={editing}
              draft={draft}
              onDraft={setDraft}
              cleanRef={cleanRef}
              taRef={taRef}
              selection={sel}
              onMouseUp={handleMouseUp}
              onHoverSpan={onHoverSpan}
              onLeaveSpan={onLeaveSpan}
              activeHighlightId={activeHighlightId}
              focusedTermId={focusedTermId}
              onFocusSpan={onFocusSpan}
              readOnly={readOnly}
              onAddGlossary={() => { onAddGlossary(block.block_id, sel); clearSelection(); }}
              onAddEntity={() => { onAddEntity(block.block_id, sel); clearSelection(); }}
            />
            <TranslationCompare
              translations={block.translations}
              block={block}
              onHoverSpan={onHoverSpan}
              onLeaveSpan={onLeaveSpan}
              activeHighlightId={activeHighlightId}
              focusedTermId={focusedTermId}
              onFocusSpan={onFocusSpan}
            />

            {!editing && !readOnly && (
              <div className="clean-hint">
                <Ic.tag size={11} />{uiText("Chọn văn bản để thêm lần xuất hiện thuật ngữ hoặc lần nhắc thực thể.", "Select text to add a glossary occurrence or entity mention.")}
                <span className="hint-keys"><span className="kbd">⌘</span><span className="kbd">↵</span> {uiText("đánh dấu đã duyệt", "mark reviewed")}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function ChapterStream({
  blocks = [], chapters = [], selectedId, review, getSpansForBlock, onSelectBlock, onCommitClean,
  onMarkReviewed, onAddGlossary, onAddEntity, onHoverSpan, onLeaveSpan, readOnly, activeHighlightId,
  focusedTermId, onFocusSpan
}) {
  const rows = blocks || [];
  const chapterLookup = React.useMemo(() => {
    const map = {};
    (chapters || []).forEach(ch => { map[ch.chapter_id] = ch; });
    return map;
  }, [chapters]);
  const chapterCounts = React.useMemo(() => {
    const map = {};
    rows.forEach(row => { map[row.chapter_id] = (map[row.chapter_id] || 0) + 1; });
    return map;
  }, [rows]);
  const scrollRef = React.useRef(null);
  React.useEffect(() => {
    if (!selectedId || !scrollRef.current) return;
    const row = Array.from(scrollRef.current.querySelectorAll("[data-block-id]"))
      .find(el => el.dataset.blockId === selectedId);
    if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [selectedId, rows.length]);

  return (
    <div className="ed-scroll" ref={scrollRef}>
      <div className="chapter-stream">
        {rows.map((row, index) => {
          const prev = rows[index - 1];
          const startsChapter = index === 0 || prev?.chapter_id !== row.chapter_id;
          const chapter = chapterLookup[row.chapter_id] || {};
          const title = chapter.title || chapter.chapter_title || row.chapter_id;
          const count = chapterCounts[row.chapter_id] || 0;
          return (
            <React.Fragment key={row.block_id}>
              {startsChapter && (
                <div className="chapter-divider" data-chapter-id={row.chapter_id}>
                  <span className="chapter-divider-rule" />
                  <span className="chapter-divider-title">{title}</span>
                  <span className="chapter-divider-meta mono">{row.chapter_id} · {count} block</span>
                </div>
              )}
              <ChapterBlockRow
                block={row}
                spans={getSpansForBlock(row)}
                reviewed={!!review?.blocks?.[row.block_id]?.reviewed}
                active={row.block_id === selectedId}
                onSelectBlock={onSelectBlock}
                onCommitClean={onCommitClean}
                onMarkReviewed={onMarkReviewed}
                onAddGlossary={onAddGlossary}
                onAddEntity={onAddEntity}
                onHoverSpan={onHoverSpan}
                onLeaveSpan={onLeaveSpan}
                readOnly={readOnly}
                activeHighlightId={activeHighlightId}
                focusedTermId={focusedTermId}
                onFocusSpan={onFocusSpan}
              />
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
}

function FocusTermChip({ term, index = 0, onJump, onClear }) {
  if (!term?.id) return null;
  const count = Number(term.count || 0);
  return (
    <div className="focus-chip" role="status">
      <span className="focus-chip-title">
        <Ic.tag size={12} />
        <b>{term.source || term.id}</b>
        {term.target && <><span className="focus-chip-arrow">→</span><span>{term.target}</span></>}
      </span>
      <span className="focus-chip-count">{count ? `${Math.min(index + 1, count)}/${count}` : "0/0"}</span>
      <button className="btn icon-only sm" onClick={() => onJump && onJump(-1)} disabled={!count} aria-label={uiText("Lần xuất hiện trước", "Previous occurrence")}>‹</button>
      <button className="btn icon-only sm" onClick={() => onJump && onJump(1)} disabled={!count} aria-label={uiText("Lần xuất hiện tiếp theo", "Next occurrence")}><Ic.chevRight size={12} /></button>
      <button className="btn icon-only sm" onClick={onClear} aria-label={uiText("Bỏ tập trung", "Clear focus")}><Ic.x size={12} /></button>
    </div>
  );
}

function OverlayLegend() {
  return (
    <div className="overlay-legend" aria-label={uiText("Chú giải đánh dấu runtime", "Runtime mark legend")}>
      <span><i className="legend-swatch localized" />{uiText("Xanh: EN đã định vị / VI khớp chuẩn", "Green: EN located / VI matches canonical")}</span>
      <span><i className="legend-swatch localization-source-warning" />{uiText("Vàng: EN có ít nhất một bản VI lệch", "Yellow: EN has at least one mismatched VI version")}</span>
      <span><i className="legend-swatch localization-mismatch" />{uiText("Đỏ: VI lệch bản dịch chuẩn", "Red: VI differs from canonical translation")}</span>
    </div>
  );
}

function addressSummary(address) {
  if (!address) return null;
  const selfTerm = address.self_term || address.self || "";
  const addressTerm = address.address_term || address.address || "";
  const pair = address.pair || "";
  const relationId = address.relation_id || "";
  const terms = [selfTerm, addressTerm].filter(Boolean).join(" / ");
  return {
    text: terms || pair || relationId || "address applied",
    sub: [pair, relationId].filter(Boolean).join(" · "),
  };
}

function previewMentionMeta(mention) {
  if (!mention || typeof mention !== "object") return null;
  if (mention.entity_id) return { kind: "entity", id: String(mention.entity_id) };
  if (mention.term_id) return { kind: "term", id: String(mention.term_id) };
  return null;
}

function rangesOverlap(aStart, aEnd, bStart, bEnd) {
  return aStart < bEnd && bStart < aEnd;
}

function findPreviewRanges(text, mentions = [], surfaceKey) {
  const value = String(text || "");
  if (!value || !Array.isArray(mentions) || !mentions.length) return [];
  const occupied = [];
  const candidates = mentions
    .map((mention, index) => {
      const meta = previewMentionMeta(mention);
      const surface = String(mention?.[surfaceKey] || "");
      if (!meta || !surface) return null;
      return {
        mention,
        index,
        kind: meta.kind,
        id: meta.id,
        surface,
        sourceSurface: String(mention.source_surface || ""),
        targetSurface: String(mention.target_surface || ""),
      };
    })
    .filter(Boolean)
    .sort((a, b) => b.surface.length - a.surface.length || a.index - b.index);

  const ranges = [];
  candidates.forEach(item => {
    let from = 0;
    while (from <= value.length) {
      const start = value.indexOf(item.surface, from);
      if (start === -1) break;
      const end = start + item.surface.length;
      if (!occupied.some(range => rangesOverlap(start, end, range.start, range.end))) {
        const kindLabel = item.kind === "entity" ? "entity" : "term";
        occupied.push({ start, end });
        ranges.push({
          start,
          end,
          ...item,
          title: `${kindLabel} ${item.id}: ${item.sourceSurface || "source?"} -> ${item.targetSurface || "target?"}`,
        });
        break;
      }
      from = start + Math.max(1, item.surface.length);
    }
  });

  return ranges.sort((a, b) => a.start - b.start || b.end - a.end);
}

function renderPreviewText(text, mentions, surfaceKey) {
  const value = String(text || "");
  const ranges = findPreviewRanges(value, mentions, surfaceKey);
  if (!ranges.length) return value;
  const pieces = [];
  let cursor = 0;
  ranges.forEach((range, index) => {
    if (range.start > cursor) pieces.push(<span key={`t-${index}`}>{value.slice(cursor, range.start)}</span>);
    pieces.push(
      <mark
        key={`h-${index}`}
        className={"tp-hl " + range.kind}
        title={range.title}
        aria-label={range.title}
      >
        {value.slice(range.start, range.end)}
      </mark>
    );
    cursor = range.end;
  });
  if (cursor < value.length) pieces.push(<span key="tail">{value.slice(cursor)}</span>);
  return pieces;
}

function prettyJson(value) {
  return JSON.stringify(value || {}, null, 2);
}

async function copyPlainText(text) {
  const value = String(text || "");
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const node = document.createElement("textarea");
  node.value = value;
  node.setAttribute("readonly", "");
  node.style.position = "fixed";
  node.style.opacity = "0";
  document.body.appendChild(node);
  node.select();
  document.execCommand("copy");
  document.body.removeChild(node);
}

function downloadJsonFile(filename, data) {
  const blob = new Blob([prettyJson(data)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function translationPreviewPaths(inputBundle, docId, chapterId) {
  const paths = inputBundle?.paths || {};
  const safeChapter = (chapterId || "").replace(/[^A-Za-z0-9_-]/g, "_") || "chapter";
  const fallbackRoot = docId ? `ailab_projects/${docId}` : "";
  return {
    project_root: paths.project_root || fallbackRoot,
    input: paths.input || (fallbackRoot ? `${fallbackRoot}/working/translation_preview/input/${safeChapter}_input.json` : ""),
    preview_output_suggested: paths.preview_output_suggested || (fallbackRoot ? `${fallbackRoot}/working/translation_preview/agent_outputs/${safeChapter}_preview.json` : ""),
    runs_dir: paths.runs_dir || (fallbackRoot ? `${fallbackRoot}/working/translation_preview/runs` : ""),
  };
}

function translationPreviewPrompt(docId, chapterId, inputBundle) {
  const paths = translationPreviewPaths(inputBundle, docId, chapterId);
  return [
    "You are the AI-LAB Translation Preview Agent.",
    "",
    "Read first:",
    "1. skills/dataset-translation-preview/SKILL.md",
    "2. skills/dataset-translation-preview/references/TRANSLATION_PREVIEW_CONTRACT.md",
    "",
    "Current source:",
    `- doc_id: ${docId || "<doc_id>"}`,
    `- chapter_id: ${chapterId || "<chapter_id>"}`,
    `- project folder: ${paths.project_root || ""}`,
    `- translation_input: ${paths.input || ""}`,
    `- preview_output_suggested: ${paths.preview_output_suggested || ""}`,
    "",
    "Requirements:",
    "- Read the input bundle JSON from translation_input above as UTF-8 (it contains Vietnamese; in Python use open(path, encoding=\"utf-8\")).",
    "- Translate only blocks included in the bundle.",
    "- Obey canonical_target / expected_target / forbidden_variants.",
    "- Apply address_policy when discourse speaker/addressee and relation match.",
    "- Do not create span/start/end/offset.",
    "- Do not write canonical/gold/manual_reference_subset.",
    "- Output exactly one JSON object following the contract. Do not wrap it in markdown.",
    "",
    "Output:",
    "- Preferred: return exactly one JSON object to paste back into the web tool (the paste path is always UTF-8 safe).",
    "- If you write the preview to a file (preview_output_suggested above), you MUST write it as UTF-8 WITHOUT BOM. Do NOT use the OS default encoding / ANSI / cp1252 — on Windows that replaces every Vietnamese diacritic with \"?\" and destroys the text. In Python: open(path, \"w\", encoding=\"utf-8\"). Never use Out-File/Set-Content without -Encoding utf8.",
    "- After writing a file, verify it still contains real Vietnamese letters (e.g. \"ông\", \"ngài\"), not \"?\".",
    "- The JSON is imported back with Load preview file or paste."
  ].join("\n");
}

async function decodeUploadedFile(file) {
  // Robust decode. PowerShell ">" / Out-File default to UTF-16LE with a BOM, and
  // some editors add a UTF-8 BOM. file.text() assumes UTF-8 and mangles those, so
  // sniff the byte-order mark and decode accordingly. This rescues Vietnamese from
  // UTF-16/BOM saves (their bytes are intact, only the decoding was wrong).
  const buf = new Uint8Array(await file.arrayBuffer());
  let encoding = "utf-8";
  let start = 0;
  if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) { encoding = "utf-16le"; start = 2; }
  else if (buf.length >= 2 && buf[0] === 0xfe && buf[1] === 0xff) { encoding = "utf-16be"; start = 2; }
  else if (buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) { encoding = "utf-8"; start = 3; }
  return new TextDecoder(encoding).decode(buf.subarray(start));
}

function countMojibakeMarks(text) {
  // A "?" glued to a letter (e.g. "?ng", "tr??ng") is almost never a real question
  // mark — it is the cp1252/ANSI-save signature where Vietnamese diacritics were
  // replaced by "?". A real "?" is followed by space/quote/end, not by a letter.
  return (String(text || "").match(/\?(?=[A-Za-z])/g) || []).length;
}

function TranslationPreviewView({
  docInfo, chapters = [], allBlocks = [], chapter, selectedId, onSelectBlock, linkIndex, onPreviewRunChange
}) {
  const [runs, setRuns] = React.useState([]);
  const [selectedChapterId, setSelectedChapterId] = React.useState(chapter?.chapter_id || chapters[0]?.chapter_id || "");
  const [selectedRunId, setSelectedRunId] = React.useState("");
  const [loadedRun, setLoadedRun] = React.useState(null);
  const [loadingRuns, setLoadingRuns] = React.useState(false);
  const [loadingRun, setLoadingRun] = React.useState(false);
  const [error, setError] = React.useState("");
  const [inputBundle, setInputBundle] = React.useState(null);
  const [inputText, setInputText] = React.useState("");
  const [inputLoading, setInputLoading] = React.useState(false);
  const [importText, setImportText] = React.useState("");
  const [importing, setImporting] = React.useState(false);
  const [loopNotice, setLoopNotice] = React.useState("");
  const [importWarnings, setImportWarnings] = React.useState([]);
  const importFileRef = React.useRef(null);
  const docId = docInfo?.doc_id || "";

  React.useEffect(() => {
    if (typeof onPreviewRunChange === "function") {
      onPreviewRunChange(loadedRun ? { run: loadedRun, chapter_id: selectedChapterId } : null);
    }
  }, [loadedRun, selectedChapterId, onPreviewRunChange]);

  React.useEffect(() => {
    const next = chapter?.chapter_id || chapters[0]?.chapter_id || "";
    setSelectedChapterId(next);
  }, [chapter?.chapter_id, chapters.length]);

  React.useEffect(() => {
    let alive = true;
    if (!docId) return;
    setLoadingRuns(true);
    setError("");
    setLoadedRun(null);
    window.AILAB_API.listTranslationPreviewRuns(docId)
      .then(data => {
        if (!alive) return;
        setRuns(data.runs || []);
      })
      .catch(err => {
        if (!alive) return;
        setRuns([]);
        setError(err?.message || "Cannot load translation preview runs.");
      })
      .finally(() => alive && setLoadingRuns(false));
    return () => { alive = false; };
  }, [docId]);

  const chapterRuns = React.useMemo(
    () => (runs || []).filter(run => run.chapter_id === selectedChapterId),
    [runs, selectedChapterId]
  );

  React.useEffect(() => {
    if (!chapterRuns.length) {
      setSelectedRunId("");
      setLoadedRun(null);
      return;
    }
    if (!chapterRuns.some(run => run.run_id === selectedRunId)) {
      setSelectedRunId(chapterRuns[chapterRuns.length - 1].run_id);
    }
  }, [chapterRuns, selectedRunId]);

  React.useEffect(() => {
    let alive = true;
    if (!docId || !selectedRunId) return;
    setLoadingRun(true);
    setError("");
    window.AILAB_API.loadTranslationPreviewRun(docId, selectedRunId)
      .then(data => {
        if (!alive) return;
        setLoadedRun(data.run || null);
      })
      .catch(err => {
        if (!alive) return;
        setLoadedRun(null);
        setError(err?.message || "Cannot load translation preview run.");
      })
      .finally(() => alive && setLoadingRun(false));
    return () => { alive = false; };
  }, [docId, selectedRunId]);

  const chapterRows = React.useMemo(
    () => (allBlocks || []).filter(block => block.chapter_id === selectedChapterId),
    [allBlocks, selectedChapterId]
  );

  const runByBlock = React.useMemo(() => {
    const map = {};
    (loadedRun?.blocks || []).forEach(row => { if (row.block_id) map[row.block_id] = row; });
    return map;
  }, [loadedRun]);

  const currentChapter = chapters.find(ch => ch.chapter_id === selectedChapterId) || {};
  const warnings = loadedRun?.warnings || [];
  const inputPaths = React.useMemo(
    () => translationPreviewPaths(inputBundle, docId, selectedChapterId),
    [inputBundle, docId, selectedChapterId]
  );

  React.useEffect(() => {
    let alive = true;
    if (!docId || !selectedChapterId) {
      setInputBundle(null);
      setInputText("");
      return () => { alive = false; };
    }
    setInputLoading(true);
    window.AILAB_API.getSavedTranslationPreviewInput(docId, selectedChapterId)
      .then(data => {
        if (!alive) return;
        setInputBundle(data);
        setInputText(prettyJson(data));
      })
      .catch(err => {
        if (!alive) return;
        if (err?.status !== 404) {
          setError(err?.message || "Cannot load saved translation preview input.");
        }
        setInputBundle(null);
        setInputText("");
      })
      .finally(() => alive && setInputLoading(false));
    return () => { alive = false; };
  }, [docId, selectedChapterId]);

  function changeChapter(chapterId) {
    setSelectedChapterId(chapterId);
    setInputBundle(null);
    setInputText("");
    setLoopNotice("");
    setImportWarnings([]);
    const first = (allBlocks || []).find(block => block.chapter_id === chapterId);
    if (first) onSelectBlock(first.block_id);
  }

  async function refreshRuns(selectRunId) {
    const data = await window.AILAB_API.listTranslationPreviewRuns(docId);
    setRuns(data.runs || []);
    if (selectRunId) setSelectedRunId(selectRunId);
  }

  async function buildInputBundle() {
    if (!docId || !selectedChapterId) return;
    setInputLoading(true);
    setError("");
    setLoopNotice("");
    setImportWarnings([]);
    try {
      const data = await window.AILAB_API.getTranslationPreviewInput(docId, selectedChapterId);
      setInputBundle(data);
      setInputText(prettyJson(data));
      setLoopNotice(`Input bundle built and saved: ${data.paths?.input || "working/translation_preview/input"}.`);
    } catch (err) {
      setInputBundle(null);
      setInputText("");
      setError(err?.message || "Cannot build translation preview input.");
    } finally {
      setInputLoading(false);
    }
  }

  async function copyInputJson() {
    if (!inputText) return;
    await copyPlainText(inputText);
    setLoopNotice("Input JSON copied.");
  }

  async function copyInputPath() {
    if (!inputPaths.input) return;
    await copyPlainText(inputPaths.input);
    setLoopNotice("Input path copied.");
  }

  async function copyPrompt() {
    await copyPlainText(translationPreviewPrompt(docId, selectedChapterId, inputBundle));
    setLoopNotice("Translation preview prompt copied.");
  }

  function downloadInputJson() {
    if (!inputBundle) return;
    downloadJsonFile(`${docId || "doc"}_${selectedChapterId || "chapter"}_translation_input.json`, inputBundle);
  }

  async function importPreviewRun() {
    setImporting(true);
    setError("");
    setLoopNotice("");
    setImportWarnings([]);
    try {
      const parsed = JSON.parse(importText || "{}");
      const data = await window.AILAB_API.importTranslationPreviewRun(docId, { preview: parsed });
      const warnings = data.warnings || [];
      setImportWarnings(warnings);
      await refreshRuns(data.run?.run_id);
      setLoadedRun(data.run || null);
      setLoopNotice(uiText(`Đã nhập lần chạy xem trước: ${data.run?.run_id || "lần chạy mới"}.`, `Preview run imported: ${data.run?.run_id || "new run"}.`));
    } catch (err) {
      if (err instanceof SyntaxError) {
        setError(uiText("JSON xem trước không hợp lệ.", "Preview JSON is invalid."));
      } else {
        setError(err?.message || uiText("Không thể nhập lần chạy xem trước bản dịch.", "Cannot import translation preview run."));
      }
    } finally {
      setImporting(false);
    }
  }

  async function loadAgentPreviewRun() {
    if (!docId || !selectedChapterId) return;
    setImporting(true);
    setError("");
    setLoopNotice("");
    setImportWarnings([]);
    try {
      const data = await window.AILAB_API.importAgentTranslationPreviewRun(docId, selectedChapterId);
      const warnings = data.warnings || [];
      setImportWarnings(warnings);
      await refreshRuns(data.run?.run_id);
      setLoadedRun(data.run || null);
      setLoopNotice(uiText(`Đã nhập bản xem trước của agent: ${data.run?.run_id || "lần chạy mới"}.`, `Agent preview imported: ${data.run?.run_id || "new run"}.`));
    } catch (err) {
      setError(err?.message || uiText("Không thể tải đầu ra xem trước của agent.", "Cannot load agent preview output."));
    } finally {
      setImporting(false);
    }
  }

  async function loadImportFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const text = await decodeUploadedFile(file);
      setImportText(text);
      const mojibake = countMojibakeMarks(text);
      if (mojibake >= 8) {
        setLoopNotice("");
        setError(
          `"${file.name}" looks corrupted: ${mojibake} Vietnamese marks were replaced by "?" ` +
          "(the file was saved as ANSI/cp1252, not UTF-8). Diacritics cannot be recovered from this " +
          "file — re-save the agent output as UTF-8 (VS Code: \"Save with Encoding -> UTF-8\", or paste " +
          "the JSON straight into the box above), then load again."
        );
      } else {
        setError("");
        setLoopNotice(uiText(`Đã tải file JSON xem trước: ${file.name}.`, `Loaded preview JSON file: ${file.name}.`));
      }
    } catch (err) {
      setError(err?.message || uiText("Không thể đọc file JSON xem trước.", "Cannot read preview JSON file."));
    } finally {
      event.target.value = "";
    }
  }

  return (
    <div className="ed-scroll">
      <div className="translation-preview">
        <div className="tp-controls">
          <label className="tp-control">
            <span>{uiText("Chương", "Chapter")}</span>
            <select value={selectedChapterId} onChange={e => changeChapter(e.target.value)}>
              {chapters.map(ch => (
                <option key={ch.chapter_id} value={ch.chapter_id}>
                  {ch.title || ch.chapter_title || ch.chapter_id}
                </option>
              ))}
            </select>
          </label>
          <label className="tp-control wide">
            <span>{uiText("Lần chạy xem trước", "Preview run")}</span>
            <select value={selectedRunId} onChange={e => setSelectedRunId(e.target.value)} disabled={!chapterRuns.length || loadingRuns}>
              {!chapterRuns.length && <option value="">{uiText("Chương này chưa có lần chạy xem trước", "No preview run for this chapter")}</option>}
              {chapterRuns.map(run => <option key={run.run_id} value={run.run_id}>{previewRunLabel(run)}</option>)}
            </select>
          </label>
          <div className="tp-run-meta mono">
            {loadedRun ? `${loadedRun.run_id} · ${loadedRun.skill_version || "unknown skill"}` : loadingRuns || loadingRun ? "loading..." : "no run loaded"}
          </div>
        </div>

        <div className="tp-loop-panel">
          <div className="tp-loop-head">
            <div>
              <b>{uiText("Vòng lặp xem trước", "Preview loop")}</b>
              <span>{uiText("Tạo gói đầu vào, chạy skill bên ngoài ứng dụng, rồi nhập JSON xem trước tại đây.", "Build an input bundle, run the skill outside the app, then import the preview JSON here.")}</span>
            </div>
            <div className="tp-loop-actions">
              <button className="btn" onClick={buildInputBundle} disabled={!selectedChapterId || inputLoading}>
                <Ic.layers size={13} /> {inputLoading ? uiText("Đang tạo...", "Building...") : uiText("Tạo đầu vào", "Build input")}
              </button>
              <button className="btn" onClick={copyPrompt} disabled={!inputBundle}>
                <Ic.sparkle size={13} /> {uiText("Sao chép prompt", "Copy prompt")}
              </button>
              <button className="btn" onClick={copyInputPath} disabled={!inputPaths.input || !inputBundle}>
                <Ic.folder size={13} /> {uiText("Sao chép đường dẫn", "Copy path")}
              </button>
              <button className="btn" onClick={copyInputJson} disabled={!inputText}>
                <Ic.doc size={13} /> {uiText("Sao chép JSON", "Copy JSON")}
              </button>
              <button className="btn" onClick={downloadInputJson} disabled={!inputBundle}>
                <Ic.upload size={13} /> {uiText("Tải JSON", "Download JSON")}
              </button>
            </div>
          </div>
          {inputText && (
            <textarea
              className="tp-json-textarea"
              value={inputText}
              readOnly
              aria-label={uiText("JSON đầu vào xem trước bản dịch", "Translation preview input JSON")}
            />
          )}
          <div className="tp-import-box">
            <textarea
              className="tp-json-textarea compact"
              value={importText}
              onChange={e => setImportText(e.target.value)}
              placeholder={uiText("Dán JSON đầu ra xem trước bản dịch tại đây...", "Paste translation preview output JSON here...")}
              aria-label={uiText("JSON đầu ra xem trước bản dịch", "Translation preview output JSON")}
            />
            <div className="tp-import-actions">
              <button className="btn" onClick={loadAgentPreviewRun} disabled={!selectedChapterId || importing}>
                <Ic.sparkle size={13} /> {uiText("Tải bản xem trước agent", "Load agent preview")}
              </button>
              <button className="btn" onClick={() => importFileRef.current?.click()}>
                <Ic.upload size={13} /> {uiText("Tải file xem trước", "Load preview file")}
              </button>
              <input
                ref={importFileRef}
                type="file"
                accept="application/json,.json"
                className="hidden-file"
                onChange={loadImportFile}
              />
              <button className="btn primary" onClick={importPreviewRun} disabled={!importText.trim() || importing}>
                <Ic.checkCircle size={13} /> {importing ? uiText("Đang nhập...", "Importing...") : uiText("Nhập bản xem trước", "Import preview")}
              </button>
            </div>
          </div>
          {loopNotice && <div className="tp-loop-note good"><Ic.checkSmall size={12} />{loopNotice}</div>}
          {importWarnings.length > 0 && (
            <div className="tp-loop-note warn">
              <Ic.alert size={12} />
              <span>{uiText(`${importWarnings.length} cảnh báo`, `${importWarnings.length} warning${importWarnings.length > 1 ? "s" : ""}`)}: {importWarnings.slice(0, 3).map(item => item.code).join(", ")}</span>
            </div>
          )}
        </div>

        {error && <div className="tp-warning bad"><Ic.xCircle size={13} />{error}</div>}
        {warnings.length > 0 && (
          <div className="tp-warning">
            <Ic.alert size={13} />
            <div>
              <b>{uiText(`${warnings.length} cảnh báo khi nhập`, `${warnings.length} import warning${warnings.length > 1 ? "s" : ""}`)}</b>
              <div className="tp-warning-list">
                {warnings.slice(0, 4).map((warning, index) => (
                  <span key={index}>{warning.code}: {warning.context_id || warning.relation_id || warning.block_id || warning.message}</span>
                ))}
                {warnings.length > 4 && <span>+{warnings.length - 4} {uiText("nữa", "more")}</span>}
              </div>
            </div>
          </div>
        )}

        {!loadingRuns && !chapterRuns.length ? (
          <div className="tp-empty">
            <Ic.file size={22} />
            <div>{uiText("Không có lần chạy xem trước bản dịch cho", "No translation preview run for")} {currentChapter.title || selectedChapterId || uiText("chương này", "this chapter")}.</div>
            <p>{uiText("Nhập một lần chạy JSON qua API S2, rồi tải lại chế độ này.", "Import a JSON run through the S2 API, then reload this view.")}</p>
          </div>
        ) : (
          <div className="tp-table">
            <div className="tp-table-head">
              <span>{uiText("Nguồn EN", "Source EN")}</span>
              <span>{uiText("Xem trước VI", "Preview VI")}</span>
            </div>
            {chapterRows.map(block => {
              const preview = runByBlock[block.block_id] || null;
              const address = addressSummary(preview?.address_applied);
              const usedContext = preview?.used_context || [];
              const mentions = preview?.mentions || [];
              return (
                <article
                  key={block.block_id}
                  className={"tp-row" + (selectedId === block.block_id ? " active" : "")}
                  data-block-id={block.block_id}
                  onMouseDown={() => onSelectBlock(block.block_id)}
                >
                  <div className="tp-cell tp-source">
                    <div className="tp-block-meta">
                      <span className="mono">{block.block_id}</span>
                      <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
                    </div>
                    <div className={"tp-text" + (block.block_type === "prose" ? " tp-text-flow" : "")}>{renderPreviewText(block.clean_text || "", mentions, "source_surface")}</div>
                  </div>
                  <div className={"tp-cell tp-target" + (!preview?.target_text ? " missing" : "")}>
                    <div className="tp-target-head">
                      <span className="mono">{preview ? uiText("khớp theo block_id", "matched by block_id") : uiText("thiếu bản xem trước", "missing preview")}</span>
                      {address && <span className="tp-address"><Ic.users size={12} />{address.text}{address.sub ? <em>{address.sub}</em> : null}</span>}
                    </div>
                    <div className="tp-text">{preview?.target_text ? renderPreviewText(preview.target_text, mentions, "target_surface") : uiText("(chưa dịch trong lần chạy này)", "(not translated in this run)")}</div>
                    {(usedContext.length > 0 || preview?.notes) && (
                      <div className="tp-context">
                        {usedContext.map((id, idx) => {
                          const chip = contextChip(id, linkIndex);
                          return <span key={`${id}:${idx}`} className={"tp-chip " + chip.kind} title={chip.title}>{chip.label}</span>;
                        })}
                        {preview?.notes && <span className="tp-note">{preview.notes}</span>}
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function storedTranslationText(row) {
  return String(row?.target_text || row?.output_text || "");
}

function storedTranslationMeta(row) {
  return [row?.model, row?.prompt_version || row?.stage].filter(Boolean).join(" / ");
}

function TranslationResultsView({
  docInfo, chapters = [], allBlocks = [], chapter, selectedId, onSelectBlock, onPreviewRunChange
}) {
  const [selectedChapterId, setSelectedChapterId] = React.useState(chapter?.chapter_id || chapters[0]?.chapter_id || "");
  const [primaryConfig, setPrimaryConfig] = React.useState("");
  const [secondaryConfig, setSecondaryConfig] = React.useState("");

  React.useEffect(() => {
    setSelectedChapterId(chapter?.chapter_id || chapters[0]?.chapter_id || "");
  }, [chapter?.chapter_id, chapters.length]);

  const chapterRows = React.useMemo(
    () => (allBlocks || []).filter(block => block.chapter_id === selectedChapterId),
    [allBlocks, selectedChapterId]
  );

  const configRows = React.useMemo(() => {
    const byConfig = new Map();
    chapterRows.forEach(block => {
      Object.entries(block.translations || {}).forEach(([config, row]) => {
        if (!row) return;
        const current = byConfig.get(config) || { config, translated: 0, sample: row };
        if (storedTranslationText(row)) current.translated += 1;
        byConfig.set(config, current);
      });
    });
    return [...byConfig.values()].sort((a, b) => a.config.localeCompare(b.config));
  }, [chapterRows]);

  const configKeys = React.useMemo(() => configRows.map(row => row.config), [configRows]);
  const configSignature = configKeys.join("\u0000");

  React.useEffect(() => {
    setPrimaryConfig(current => configKeys.includes(current) ? current : (configKeys[0] || ""));
  }, [configSignature]);

  React.useEffect(() => {
    if (configKeys.length < 2) {
      setSecondaryConfig("");
      return;
    }
    setSecondaryConfig(current => (
      current && current !== primaryConfig && configKeys.includes(current)
        ? current
        : (configKeys.find(config => config !== primaryConfig) || "")
    ));
  }, [configSignature, primaryConfig]);

  const visibleConfigs = React.useMemo(
    () => [primaryConfig, secondaryConfig].filter((value, index, rows) => value && rows.indexOf(value) === index),
    [primaryConfig, secondaryConfig]
  );
  const visibleSignature = visibleConfigs.join("\u0000");

  React.useEffect(() => {
    if (typeof onPreviewRunChange !== "function") return;
    if (!visibleConfigs.length) {
      onPreviewRunChange(null);
      return;
    }
    const exportedBlocks = chapterRows.map(block => ({
      block_id: block.block_id,
      source_text: block.clean_text || block.source_text || "",
      translations: Object.fromEntries(visibleConfigs.map(config => [config, block.translations?.[config] || null])),
    }));
    onPreviewRunChange({
      chapter_id: selectedChapterId,
      run: {
        run_id: visibleConfigs.join("_vs_"),
        doc_id: docInfo?.doc_id || "",
        chapter_id: selectedChapterId,
        configs: visibleConfigs,
        block_count: chapterRows.length,
        translated_block_count: exportedBlocks.filter(block => Object.values(block.translations).some(row => storedTranslationText(row))).length,
        blocks: exportedBlocks,
      },
    });
  }, [docInfo?.doc_id, selectedChapterId, chapterRows, visibleSignature, onPreviewRunChange]);

  function changeChapter(chapterId) {
    setSelectedChapterId(chapterId);
    const first = (allBlocks || []).find(block => block.chapter_id === chapterId);
    if (first) onSelectBlock(first.block_id);
  }

  const currentChapter = chapters.find(ch => ch.chapter_id === selectedChapterId) || {};
  const columns = Math.max(2, 1 + visibleConfigs.length);

  return (
    <div className="ed-scroll">
      <div className="translation-preview">
        <div className="tp-controls">
          <label className="tp-control wide">
            <span>{uiText("Chương", "Chapter")}</span>
            <select value={selectedChapterId} onChange={event => changeChapter(event.target.value)}>
              {chapters.map(row => (
                <option key={row.chapter_id} value={row.chapter_id}>
                  {row.title || row.chapter_title || row.chapter_id}
                </option>
              ))}
            </select>
          </label>
          {configKeys.length === 1 ? (
            <div className="tp-single-config">
              <span>{uiText("Bản dịch", "Translation")}</span>
              <b className="mono">{configKeys[0]}</b>
            </div>
          ) : configKeys.length > 1 ? (<>
            <label className="tp-control">
              <span>{uiText("Phiên bản A", "Version A")}</span>
              <select value={primaryConfig} onChange={event => setPrimaryConfig(event.target.value)}>
                {configKeys.map(config => <option key={config} value={config}>{config}</option>)}
              </select>
            </label>
            <label className="tp-control">
              <span>{uiText("Phiên bản B", "Version B")}</span>
              <select value={secondaryConfig} onChange={event => setSecondaryConfig(event.target.value)}>
                {configKeys.filter(config => config !== primaryConfig).map(config => (
                  <option key={config} value={config}>{config}</option>
                ))}
              </select>
            </label>
          </>) : null}
          <div className="tp-run-meta mono">
            {configKeys.length
              ? uiText(`${chapterRows.length} block / ${configKeys.length} phiên bản đã lưu`, `${chapterRows.length} blocks / ${configKeys.length} stored version${configKeys.length > 1 ? "s" : ""}`)
              : uiText("không có bản dịch đã lưu", "no stored translation")}
          </div>
        </div>

        {!configKeys.length ? (
          <div className="tp-empty">
            <Ic.file size={22} />
            <div>{uiText("Không có kết quả dịch cho", "No translation result for")} {currentChapter.title || selectedChapterId || uiText("chương này", "this chapter")}.</div>
            <p>{uiText("Kết quả sẽ xuất hiện sau khi pipeline dịch ghi lần chạy của dự án này.", "Results appear here after the translation pipeline writes this project run.")}</p>
          </div>
        ) : (
          <div className="tp-table">
            <div className="tp-table-head" style={{ "--tp-columns": `repeat(${columns}, minmax(0, 1fr))` }}>
              <span>{uiText("Nguồn", "Source")}</span>
              {visibleConfigs.map(config => <span key={config}>{uiText("Bản dịch", "Translation")} {config}</span>)}
            </div>
            {chapterRows.map(block => (
              <article
                key={block.block_id}
                className={"tp-row" + (selectedId === block.block_id ? " active" : "")}
                style={{ "--tp-columns": `repeat(${columns}, minmax(0, 1fr))` }}
                data-block-id={block.block_id}
                onMouseDown={() => onSelectBlock(block.block_id)}
              >
                <div className="tp-cell tp-source">
                  <div className="tp-block-meta">
                    <span className="mono">{block.block_id}</span>
                    <span className={"tag tag-" + block.block_type}>{block.block_type}</span>
                  </div>
                  <div className={"tp-text" + (block.block_type === "prose" ? " tp-text-flow" : "")}>{block.clean_text || block.source_text || ""}</div>
                </div>
                {visibleConfigs.map(config => {
                  const row = block.translations?.[config] || null;
                  const text = storedTranslationText(row);
                  return (
                    <div key={config} className={"tp-cell tp-target" + (!text ? " missing" : "")}>
                      <div className="tp-target-head">
                        <span className="tc-label mono">{config}</span>
                        <span className="mono">{storedTranslationMeta(row) || (row ? uiText("kết quả đã lưu", "stored result") : uiText("thiếu", "missing"))}</span>
                      </div>
                      <div className="tp-text">
                        {text
                          ? <SpanText text={text} spans={row?.target_spans || []} block={block} />
                          : uiText("(block này chưa được dịch trong phiên bản này)", "(this block was not translated in this version)")}
                      </div>
                    </div>
                  );
                })}
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function CenterEditor({
  block, docInfo, reviewed, spans, editing, mode, chapter, chapters, chapterBlocks, allBlocks,
  review, selectedId, getSpansForBlock, linkIndex, onSelectBlock, onNextUnreviewed,
  onEdit, onCommitClean, onCancelEdit,
  onChangeType, onToggleOpening, onToggleFlag, onMarkReviewed,
  onAddGlossary, onAddEntity, onPreviewRunChange, readOnly,
  observability, runControl, selectedCallId, selectedCallDetail, callDetailLoading, onSelectCall,
  onConsoleBack, onOpenConsole, onOpenReport,
  focusTerm, focusedTermId, focusedTermIndex, onFocusSpan, onClearFocus, onFocusJump,
}) {
  const [hoverInfo, setHoverInfo] = React.useState(null);
  const activeHighlightId = hoverInfo?.span?.id || null;
  const chapterTitle = chapter?.title || chapter?.chapter_title || block.chapter_id;
  const streamBlocks = mode === "book" ? (allBlocks || []) : (chapterBlocks || []);
  const streamLabel = mode === "book" ? (docInfo?.metadata?.title || docInfo?.doc_id || "Full book") : chapterTitle;
  const streamCount = mode === "book" ? (allBlocks?.length || 0) : (chapterBlocks?.length || 0);
  const handleHoverSpan = React.useCallback((span, targetBlock, rect) => {
    setHoverInfo({ span, block: targetBlock, rect });
  }, []);
  const handleLeaveSpan = React.useCallback(() => setHoverInfo(null), []);

  return (
    <div className="col col-center">
      <EditorToolbar block={block} docInfo={docInfo} reviewed={reviewed} mode={mode}
        streamLabel={streamLabel} streamCount={streamCount} onNextUnreviewed={onNextUnreviewed}
        onChangeType={onChangeType} onToggleOpening={onToggleOpening}
        onToggleFlag={onToggleFlag} onMarkReviewed={() => onMarkReviewed(block.block_id)} readOnly={readOnly} />
      {!["console", "report"].includes(mode) && <FocusTermChip term={focusTerm} index={focusedTermIndex} onJump={onFocusJump} onClear={onClearFocus} />}

      {mode === "console" ? (
        <AgentConsole runControl={runControl} onBack={onConsoleBack} onOpenReport={onOpenReport} />
      ) : mode === "report" ? (
        <AgentReport runControl={runControl} onBack={onConsoleBack} onOpenConsole={onOpenConsole} />
      ) : mode === "preview" ? (
        <TranslationResultsView
          docInfo={docInfo}
          chapters={chapters}
          allBlocks={allBlocks}
          chapter={chapter}
          selectedId={selectedId}
          onSelectBlock={onSelectBlock}
          onPreviewRunChange={onPreviewRunChange}
        />
      ) : mode !== "block" ? (
        <ChapterStream
          blocks={streamBlocks}
          chapters={chapters}
          selectedId={selectedId}
          review={review}
          getSpansForBlock={getSpansForBlock}
          onSelectBlock={onSelectBlock}
          onCommitClean={onCommitClean}
          onMarkReviewed={onMarkReviewed}
          onAddGlossary={onAddGlossary}
          onAddEntity={onAddEntity}
          onHoverSpan={handleHoverSpan}
          onLeaveSpan={handleLeaveSpan}
          activeHighlightId={activeHighlightId}
          focusedTermId={focusedTermId}
          onFocusSpan={onFocusSpan}
          readOnly={readOnly}
        />
      ) : (
        <SingleBlockView
          block={block}
          docInfo={docInfo}
          reviewed={reviewed}
          spans={spans}
          editing={editing}
          onEdit={onEdit}
          onCommitClean={onCommitClean}
          onCancelEdit={onCancelEdit}
          onAddGlossary={onAddGlossary}
          onAddEntity={onAddEntity}
          onHoverSpan={handleHoverSpan}
          onLeaveSpan={handleLeaveSpan}
          activeHighlightId={activeHighlightId}
          focusedTermId={focusedTermId}
          onFocusSpan={onFocusSpan}
          readOnly={readOnly}
        />
      )}
      <HighlightHoverCard hover={hoverInfo} linkIndex={linkIndex} />
    </div>
  );
}

window.CenterEditor = CenterEditor;
