/* ===== APP: real backend wiring, state adapter, interactions ===== */
const { useState, useEffect, useMemo, useRef, useCallback } = React;

const API = window.AILAB_API;
const STORAGE_DOC = "ailab.doc_id";
const STORAGE_USER = "ailab.user";
const STORAGE_CENTER_MODE = "ailab.center_mode";
const THESIS_PREFIX = "thesis:";
const DEFAULT_USER = "U2 · Mai";
const EDITABLE_META = new Set(["title", "author", "domain", "genre", "source_format", "license", "source_url", "contamination_risk"]);

function cloneData(value) {
  return JSON.parse(JSON.stringify(value));
}

function currentUser() {
  return localStorage.getItem(STORAGE_USER) || DEFAULT_USER;
}

function errorMessage(err) {
  const first = err?.errors?.[0] || err?.payload?.errors?.[0];
  return first?.message || err?.message || "Request failed.";
}

function firstError(err) {
  return err?.errors?.[0] || err?.payload?.errors?.[0] || {};
}

function describeExternalRef(ref) {
  if (ref.block_id) return `${ref.kind || "ref"} · ${ref.block_id} · ${ref.field || ""}`;
  if (ref.chapter_id) return `${ref.kind || "ref"} · ${ref.chapter_id} · ${ref.field || ""}`;
  return `${ref.kind || "ref"} · ${ref.field || ""}`;
}

function normalizeErrors(report) {
  if (!report) return [];
  const errors = (report.errors || []).map(e => ({ severity: "error", ...e }));
  const warnings = (report.warnings || []).map(w => ({ severity: "warning", ...w }));
  return [...errors, ...warnings];
}

function docInfoFromDocument(document) {
  const metadata = { ...(document?.metadata || {}) };
  const provenance = {
    raw_sha256: metadata.raw_sha256 || "",
    extraction_tool: metadata.extraction_tool || "",
    pipeline_version: metadata.pipeline_version || "",
    retrieved_at: metadata.retrieved_at || "",
  };
  return {
    doc_id: document?.doc_id || "",
    schema_version: document?.schema_version || "",
    metadata,
    provenance,
  };
}

function mergeReferences(dataset) {
  const canonical = (dataset.references || []).map(row => ({ ...row, canonical: true }));
  const canonicalIds = new Set(canonical.map(row => row.reference_id));
  const draftRows = Object.values(dataset.reference_drafts?.references || {})
    .filter(row => (row.status || "draft") === "draft")
    .map(row => ({
      ...row,
      status: "draft",
      canonical: false,
      reference_vi: row.reference_vi || row.draft_vi || "",
    }))
    .filter(row => !canonicalIds.has(row.reference_id));
  return [...canonical, ...draftRows];
}

function adaptDataset(dataset) {
  return {
    docInfo: docInfoFromDocument(dataset.document),
    chapters: dataset.chapters || [],
    blocks: dataset.blocks || [],
    glossary: dataset.glossary || [],
    entities: dataset.entities || [],
    relations: dataset.entity_relations || [],
    summaries: dataset.summaries || [],
    references: mergeReferences(dataset),
    review: dataset.review_state || { blocks: {}, references: {}, summaries: {} },
    jobs: dataset.jobs || [],
    history: dataset.history_state || { can_undo: false, can_redo: false, undo_top: null, redo_top: null, recent: [] },
  };
}

function thesisJobId(docId) {
  const value = String(docId || "");
  return value.startsWith(THESIS_PREFIX) ? value.slice(THESIS_PREFIX.length) : "";
}

function adaptThesisReadModel(model) {
  const meta = model.meta || {};
  const document = model.document || {};
  const metadata = {
    ...(document.metadata || {}),
    title: document.title || document.metadata?.title || document.doc_id,
    source_filename: document.source_filename || document.metadata?.source_filename || "",
    source_lang: document.source_lang || "en",
    target_lang: document.target_lang || "vi",
    read_model_source: meta.source || "thesis_sqlite_readmodel",
  };
  const displayDocId = `${THESIS_PREFIX}${meta.job_id || document.job_id || document.doc_id || "job"}`;
  const blocks = (model.blocks || []).map(block => ({
    ...block,
    clean_text: block.clean_text || block.text || block.original_text || "",
    source_text: block.source_text || block.original_text || block.text || "",
    annotations: block.annotations || {},
    quality_flags: block.quality_flags || [],
    read_only: true,
  }));
  return {
    docInfo: {
      doc_id: displayDocId,
      schema_version: metadata.schema_version || "",
      metadata,
      provenance: {
        ...(document.provenance || {}),
        read_model: meta.source || "thesis_sqlite_readmodel",
        job_id: meta.job_id,
        db_path: meta.db_path,
        runtime_memory: meta.provenance?.runtime_memory,
        eval_only: meta.provenance?.eval_only,
      },
      thesis: {
        job_id: meta.job_id,
        document_doc_id: document.doc_id,
        available_runs: meta.available_runs || [],
        counts: meta.counts || {},
      },
      read_only: true,
    },
    chapters: model.chapters || [],
    blocks,
    glossary: model.runtime_memory?.glossary_entries || [],
    entities: model.runtime_memory?.entities || [],
    relations: model.runtime_memory?.entity_relations || [],
    summaries: [],
    references: [],
    evalOnly: model.eval_only || { gold_glossary: [], references: [] },
    translations: model.translations || {},
    review: { blocks: {}, references: {}, summaries: {} },
    jobs: meta.available_runs || [],
    history: { can_undo: false, can_redo: false, undo_top: null, redo_top: null, recent: [] },
    errors: [],
  };
}

/* build annotation spans for a block from glossary + entities, with stale detection */
function buildSpans(block, glossary, entities) {
  if (!block) return [];
  const spans = [];
  glossary.forEach(t => (t.occurrences || []).forEach(o => {
    if (o.block_id !== block.block_id) return;
    const cur = block.clean_text.slice(o.span[0], o.span[1]);
    spans.push({
      start: o.span[0],
      end: o.span[1],
      kind: "glossary",
      label: `${t.source_term} -> ${t.expected_target || "target needed"}`,
      id: t.term_id,
      stale: cur !== String(t.source_term || ""),
    });
  }));
  entities.forEach(e => (e.mentions || []).forEach(m => {
    if (m.block_id !== block.block_id) return;
    const cur = block.clean_text.slice(m.span[0], m.span[1]);
    spans.push({
      start: m.span[0],
      end: m.span[1],
      kind: "entity",
      label: `${e.canonical_source} -> ${e.canonical_target || "target needed"}`,
      id: e.entity_id,
      stale: cur !== m.surface,
    });
  }));
  return spans;
}

function Toasts({ items, onDismiss }) {
  return (
    <div className="toasts">
      {items.map(t => (
        <div key={t.id} className={"toast tn-" + (t.tone || "info")}>
          {t.tone === "good" ? <Ic.checkCircle size={14} /> : t.tone === "bad" ? <Ic.xCircle size={14} /> : <Ic.dot size={14} />}
          <div className="toast-body"><div className="toast-msg">{t.msg}</div>{t.sub && <div className="toast-sub">{t.sub}</div>}</div>
          <button className="toast-x" onClick={() => onDismiss(t.id)}><Ic.x size={11} /></button>
        </div>
      ))}
    </div>
  );
}

function Modal({ title, icon: I, tone, children, onClose, actions }) {
  return (
    <div className="modal-scrim" onMouseDown={onClose}>
      <div className="modal" onMouseDown={e => e.stopPropagation()}>
        <div className="modal-head">
          <span className={"modal-ic " + (tone || "")}>{I && <I size={16} />}</span>
          <span className="modal-title">{title}</span>
          <button className="modal-x" onClick={onClose}><Ic.x size={13} /></button>
        </div>
        <div className="modal-body">{children}</div>
        <div className="modal-foot">{actions}</div>
      </div>
    </div>
  );
}

function historyTip(prefix, event) {
  return event?.label ? `${prefix}: ${event.label}` : `${prefix} unavailable`;
}

function joinLocalPath(root, ...parts) {
  if (!root) return "";
  const sep = root.includes("\\") ? "\\" : "/";
  return [root.replace(/[\\/]+$/, ""), ...parts].filter(Boolean).join(sep);
}

function safeFilePart(value) {
  return String(value || "export").replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/^_+|_+$/g, "") || "export";
}

function downloadTextFile(filename, text, type = "text/plain;charset=utf-8") {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function downloadAppJsonFile(filename, data) {
  downloadTextFile(filename, JSON.stringify(data, null, 2), "application/json;charset=utf-8");
}

function downloadBlobFile(filename, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function pickZipSaveHandle(suggestedName) {
  if (!window.showSaveFilePicker) return null;
  try {
    return await window.showSaveFilePicker({
      suggestedName,
      types: [{ description: "ZIP archive", accept: { "application/zip": [".zip"] } }],
    });
  } catch (err) {
    if (err?.name === "AbortError") return "aborted";
    return null;
  }
}

async function writeBlobToHandle(handle, blob) {
  const writable = await handle.createWritable();
  await writable.write(blob);
  await writable.close();
}

function TopBar({ docId, dirty, lastSaved, onValidate, onExportOption, onFreeze, onUndo, onRedo, history, freezeReady, freezeReasons, previewReadOnly, canExportPreview }) {
  const [exportOpen, setExportOpen] = useState(false);
  const canUndo = !!history?.can_undo && !dirty && !previewReadOnly;
  const canRedo = !!history?.can_redo && !dirty && !previewReadOnly;
  const readOnlyTip = "Disabled in Translation Preview read-only view.";
  const packageDisabled = false;
  const qcDisabled = false;
  const previewDisabled = !canExportPreview;
  function chooseExport(kind) {
    setExportOpen(false);
    onExportOption(kind);
  }
  return (
    <div className="topbar">
      <div className="tb-left">
        <span className="tb-app"><span className="tb-logo">▧</span>AILAB <span className="tb-app-sub">Dataset Tool</span></span>
        <span className="tb-sep" />
        <span className="tb-doc tip" data-tip="Active document · one local project folder per doc_id">
          <Ic.doc size={13} className="faint" /><span className="mono">{docId}</span>
        </span>
        <span className="wc-badge tip" data-tip="Autosaved working state and canonical dataset files via backend API.">
          <span className="wc-dot" />working copy
        </span>
      </div>

      <div className="tb-right">
        <span className="autosave tip" data-tip="Writes go through the Flask backend. Freeze creates a validated versioned snapshot.">
          {dirty ? <><span className="as-spin" />saving...</> : <><Ic.check size={12} className="as-ok" />saved {lastSaved}</>}
        </span>
        <span className="tb-sep" />
        <div className="undo-group">
          <button className="btn icon-only tip" disabled={!canUndo} data-tip={previewReadOnly ? readOnlyTip : dirty ? "Wait for current save to finish" : historyTip("Undo", history?.undo_top)} onClick={onUndo} aria-label="Undo">
            <Ic.undo size={13} />
          </button>
          <button className="btn icon-only tip" disabled={!canRedo} data-tip={previewReadOnly ? readOnlyTip : dirty ? "Wait for current save to finish" : historyTip("Redo", history?.redo_top)} onClick={onRedo} aria-label="Redo">
            <Ic.redo size={13} />
          </button>
        </div>
        <span className="tb-sep" />
        <span className="user-chip tip" data-tip="Current local user"><span className="ua">M</span>{currentUser()}</span>
        <span className="tb-sep" />
        {previewReadOnly && <span className="preview-top-badge"><Ic.eye size={12} />preview read-only</span>}
        <button className="btn" disabled={previewReadOnly} data-tip={previewReadOnly ? readOnlyTip : ""} onClick={onValidate}><Ic.checkCircle size={13} />Validate</button>
        <div className="export-menu-wrap">
          <button className="btn" onClick={() => setExportOpen(v => !v)} disabled={!docId} data-tip={!docId ? "Open a project first." : ""}>
            <Ic.upload size={13} />Export<Ic.chevDown size={11} />
          </button>
          {exportOpen && (
            <div className="export-menu">
              <button disabled={packageDisabled} onClick={() => chooseExport("package")}>
                <Ic.layers size={13} />
                <span><b>Dataset package</b><em>Source + dataset + working state + QC</em></span>
              </button>
              <button disabled={qcDisabled} onClick={() => chooseExport("qc")}>
                <Ic.checkCircle size={13} />
                <span><b>QC report</b><em>Counts, validation, review, freeze gates</em></span>
              </button>
              <button onClick={() => chooseExport("dataset-previews")}>
                <Ic.book size={13} />
                <span><b>Dataset + all previews</b><em>Dataset package + all translated previews</em></span>
              </button>
              <button disabled={previewDisabled} onClick={() => chooseExport("preview")}>
                <Ic.eye size={13} />
                <span><b>Translation preview</b><em>Current preview run only, not gold</em></span>
              </button>
            </div>
          )}
        </div>
        <div className="tip tip-left" data-tip={previewReadOnly ? readOnlyTip : freezeReady ? "Create a versioned snapshot after validation and review gates pass" : "Blocked: " + freezeReasons.join(" · ")}>
          <button className="btn primary" disabled={previewReadOnly || !freezeReady} onClick={onFreeze}>
            <Ic.snow size={13} />Freeze
          </button>
        </div>
      </div>
    </div>
  );
}

function PreviewRightPanel({ docInfo, block }) {
  return (
    <div className="col col-right preview-info-panel">
      <div className="preview-info-head">
        <Ic.eye size={15} />
        <div>
          <b>Translation Preview</b>
          <span>read-only · not gold</span>
        </div>
      </div>
      <div className="preview-info-card">
        <p>This view only compares source blocks with a stored preview run.</p>
        <p>No annotation, reference, freeze, or review action is available here. Export is limited to preview-safe downloads.</p>
      </div>
      <div className="preview-info-list">
        <div><span>doc_id</span><b className="mono">{docInfo?.doc_id || ""}</b></div>
        <div><span>active block</span><b className="mono">{block?.block_id || ""}</b></div>
        <div><span>storage</span><b className="mono">working/translation_preview</b></div>
      </div>
    </div>
  );
}

function StartupState({ title, message, action, onAction, secondary }) {
  return (
    <div className="project-screen">
      <div className="project-wrap" style={{ maxWidth: 760 }}>
        <div className="project-headline">
          <div>
            <div className="project-kicker">AILAB Dataset Tool</div>
            <h1>{title}</h1>
            <p>{message}</p>
            {secondary && <p className="muted">{secondary}</p>}
          </div>
          {action && <button className="btn primary" onClick={onAction}>{action}</button>}
        </div>
      </div>
    </div>
  );
}

function App() {
  const [view, setView] = useState(() => window.location.hash === "#project" ? "project" : "workspace");
  const [projects, setProjects] = useState([]);
  const [docInfo, setDocInfo] = useState(null);
  const [chapters, setChapters] = useState([]);
  const [blocks, setBlocks] = useState([]);
  const [glossary, setGlossary] = useState([]);
  const [entities, setEntities] = useState([]);
  const [relations, setRelations] = useState([]);
  const [summaries, setSummaries] = useState([]);
  const [references, setReferences] = useState([]);
  const [evalOnly, setEvalOnly] = useState({ gold_glossary: [], references: [] });
  const [thesisTranslations, setThesisTranslations] = useState({});
  const [thesisObservability, setThesisObservability] = useState(null);
  const [selectedCallId, setSelectedCallId] = useState(null);
  const [selectedCallDetail, setSelectedCallDetail] = useState(null);
  const [callDetailLoading, setCallDetailLoading] = useState(false);
  const [review, setReview] = useState({ blocks: {}, references: {}, summaries: {} });
  const [historyState, setHistoryState] = useState({ can_undo: false, can_redo: false, undo_top: null, redo_top: null, recent: [] });
  const [errors, setErrors] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [filters, setFilters] = useState(new Set());
  const [rightOpenTabs, setRightOpenTabs] = useState(["glossary"]);
  const [editing, setEditing] = useState(false);
  const [centerMode, setCenterModeState] = useState(() => {
    const saved = localStorage.getItem(STORAGE_CENTER_MODE);
    return ["block", "chapter", "book", "preview"].includes(saved) ? saved : "chapter";
  });
  const [toasts, setToasts] = useState([]);
  const [modal, setModal] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [schemaMigrating, setSchemaMigrating] = useState(false);
  const [currentPreviewRun, setCurrentPreviewRun] = useState(null);
  const [lastSaved, setLastSaved] = useState("just now");
  const [loading, setLoading] = useState(true);
  const [bootError, setBootError] = useState(null);
  const [activeDocId, setActiveDocId] = useState(localStorage.getItem(STORAGE_DOC) || "");
  const savedAt = useRef(Date.now());
  const saveTimers = useRef({});

  const refreshProjects = useCallback(async () => {
    const [legacy, thesis] = await Promise.all([
      API.listProjects().catch(() => []),
      API.listThesisDatasets().catch(() => []),
    ]);
    const thesisRows = (thesis || []).map(row => ({
      ...row,
      doc_id: `${THESIS_PREFIX}${row.job_id}`,
      display_doc_id: row.document_doc_id || row.job_id,
      title: row.title || row.job_id,
      source: "thesis",
      status: row.status || "available",
    }));
    const list = [...(legacy || []), ...thesisRows];
    setProjects(list);
    return list;
  }, []);

  const loadDataset = useCallback(async (docId, opts = {}) => {
    if (!docId) return null;
    if (!opts.silent) setLoading(true);
    const dataset = await API.getDataset(docId);
    const adapted = adaptDataset(dataset);
    setDocInfo(adapted.docInfo);
    setChapters(adapted.chapters);
    setBlocks(adapted.blocks);
    setGlossary(adapted.glossary);
    setEntities(adapted.entities);
    setRelations(adapted.relations);
    setSummaries(adapted.summaries);
    setReferences(adapted.references);
    setEvalOnly({ gold_glossary: [], references: [] });
    setThesisTranslations({});
    setThesisObservability(null);
    setSelectedCallId(null);
    setSelectedCallDetail(null);
    setReview(adapted.review);
    setHistoryState(adapted.history);
    setActiveDocId(adapted.docInfo.doc_id);
    localStorage.setItem(STORAGE_DOC, adapted.docInfo.doc_id);
    setSelectedId(prev => adapted.blocks.some(b => b.block_id === prev) ? prev : adapted.blocks[0]?.block_id || null);
    setBootError(null);
    setLoading(false);
    return adapted;
  }, []);

  const loadThesisDataset = useCallback(async (jobId, opts = {}) => {
    if (!jobId) return null;
    if (!opts.silent) setLoading(true);
    const [dataset, observability] = await Promise.all([
      API.getThesisDataset(jobId),
      API.getThesisObservability(jobId).catch(err => ({
        meta: { source: "thesis_observability_readmodel", job_id: jobId, read_only: true, load_error: errorMessage(err) },
        calls: [],
        usage_daily: [],
        totals: { overall: { calls: 0, total_quota_tokens: 0, cost_usd: 0 } },
      })),
    ]);
    const adapted = adaptThesisReadModel(dataset);
    setDocInfo(adapted.docInfo);
    setChapters(adapted.chapters);
    setBlocks(adapted.blocks);
    setGlossary(adapted.glossary);
    setEntities(adapted.entities);
    setRelations(adapted.relations);
    setSummaries(adapted.summaries);
    setReferences(adapted.references);
    setEvalOnly(adapted.evalOnly);
    setThesisTranslations(adapted.translations);
    setThesisObservability(observability);
    setSelectedCallId(observability.calls?.[0]?.call_id || null);
    setSelectedCallDetail(null);
    setReview(adapted.review);
    setHistoryState(adapted.history);
    setErrors(adapted.errors);
    setActiveDocId(adapted.docInfo.doc_id);
    localStorage.setItem(STORAGE_DOC, adapted.docInfo.doc_id);
    setSelectedId(prev => adapted.blocks.some(b => b.block_id === prev) ? prev : adapted.blocks[0]?.block_id || null);
    setBootError(null);
    setLoading(false);
    return adapted;
  }, []);

  async function boot() {
    setLoading(true);
    setBootError(null);
    try {
      const list = await refreshProjects();
      const remembered = localStorage.getItem(STORAGE_DOC);
      const chosen = list.find(p => p.doc_id === remembered && p.status === "available")
        || list.find(p => p.status === "available")
        || list[0];
      if (!chosen) {
        setDocInfo({ doc_id: "", metadata: {}, provenance: {} });
        setView("project");
        setLoading(false);
        return;
      }
      setActiveDocId(chosen.doc_id);
      if (chosen.source === "thesis") {
        await loadThesisDataset(thesisJobId(chosen.doc_id));
      } else if (chosen.status === "available") {
        await loadDataset(chosen.doc_id);
      } else {
        setDocInfo({ doc_id: chosen.doc_id, metadata: {}, provenance: {} });
        setView("project");
        setLoading(false);
      }
    } catch (err) {
      setBootError(errorMessage(err));
      setLoading(false);
    }
  }

  useEffect(() => { boot(); }, []);

  const block = blocks.find(b => b.block_id === selectedId) || blocks[0] || null;
  const readOnly = !!docInfo?.read_only || String(activeDocId || "").startsWith(THESIS_PREFIX);

  useEffect(() => {
    if (!readOnly && centerMode === "cockpit") setCenterMode("block");
  }, [readOnly, centerMode]);

  useEffect(() => {
    const jobId = thesisJobId(activeDocId);
    if (!readOnly || !jobId || !selectedCallId) {
      setSelectedCallDetail(null);
      setCallDetailLoading(false);
      return;
    }
    let cancelled = false;
    setCallDetailLoading(true);
    API.getThesisObservabilityCall(jobId, selectedCallId)
      .then(detail => {
        if (!cancelled) setSelectedCallDetail(detail);
      })
      .catch(err => {
        if (!cancelled) {
          setSelectedCallDetail({ call_id: selectedCallId, error: errorMessage(err), messages: [], memory_pack: null });
        }
      })
      .finally(() => {
        if (!cancelled) setCallDetailLoading(false);
      });
    return () => { cancelled = true; };
  }, [activeDocId, selectedCallId, readOnly]);

  function setCenterMode(mode) {
    setCenterModeState(mode);
    localStorage.setItem(STORAGE_CENTER_MODE, mode);
  }

  function toggleRightTab(tabId) {
    setRightOpenTabs(tabs => (
      tabs.includes(tabId)
        ? tabs.filter(id => id !== tabId)
        : [...tabs, tabId]
    ));
  }

  function openRightTab(tabId) {
    setRightOpenTabs(tabs => tabs.includes(tabId) ? tabs : [...tabs, tabId]);
  }

  const touchStart = useCallback(() => {
    setDirty(true);
    savedAt.current = Date.now();
    setLastSaved("just now");
  }, []);

  const touchDone = useCallback(() => {
    savedAt.current = Date.now();
    setDirty(false);
    setLastSaved("just now");
  }, []);

  useEffect(() => {
    const t = setInterval(() => {
      const s = Math.round((Date.now() - savedAt.current) / 1000);
      setLastSaved(s < 3 ? "just now" : s < 60 ? s + "s ago" : Math.floor(s / 60) + "m ago");
    }, 4000);
    return () => clearInterval(t);
  }, []);

  function toast(msg, tone, sub) {
    const id = Math.random().toString(36).slice(2);
    setToasts(t => [...t, { id, msg, tone, sub }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 4200);
  }
  const dismiss = id => setToasts(t => t.filter(x => x.id !== id));

  async function mutate(action, { refresh = true, success, fail } = {}) {
    if (!activeDocId) return null;
    if (readOnly) {
      toast("Read-only thesis view", "info", "Pipeline SQLite data is observable only in APP-A01.");
      return null;
    }
    touchStart();
    try {
      const result = await action();
      if (refresh) await loadDataset(activeDocId, { silent: true });
      touchDone();
      if (success) toast(success, "good");
      return result;
    } catch (err) {
      touchDone();
      toast(fail || "Action failed", "bad", errorMessage(err));
      return null;
    }
  }

  function queueSave(key, action) {
    if (readOnly) {
      toast("Read-only thesis view", "info", "No workspace write is available for thesis DB data.");
      return;
    }
    touchStart();
    clearTimeout(saveTimers.current[key]);
    saveTimers.current[key] = setTimeout(async () => {
      try {
        await action();
        await loadDataset(activeDocId, { silent: true });
        touchDone();
      } catch (err) {
        touchDone();
        toast("Save failed", "bad", errorMessage(err));
      }
    }, 650);
  }

  const annoSet = useMemo(() => {
    const s = new Set();
    glossary.forEach(t => (t.occurrences || []).forEach(o => s.add(o.block_id)));
    entities.forEach(e => (e.mentions || []).forEach(m => s.add(m.block_id)));
    return s;
  }, [glossary, entities]);

  const spans = useMemo(() => buildSpans(block, glossary, entities), [block, glossary, entities]);
  const getSpansForBlock = useCallback((targetBlock) => buildSpans(targetBlock, glossary, entities), [glossary, entities]);
  const allSpans = useMemo(() => blocks.flatMap(b => buildSpans(b, glossary, entities).map(s => ({ ...s, block_id: b.block_id }))), [blocks, glossary, entities]);
  const staleCount = allSpans.filter(s => s.stale).length;

  const linkIndex = useMemo(() => {
    const blockById = {};
    const chapterById = {};
    blocks.forEach(b => { blockById[b.block_id] = b; });
    chapters.forEach(ch => { chapterById[ch.chapter_id] = ch; });

    const chapterLabel = (chapterId) => {
      const ch = chapterById[chapterId] || {};
      return ch.title || ch.chapter_title || chapterId || "";
    };

    const entityLinks = {};
    entities.forEach(entity => {
      const mentions = entity.mentions || [];
      const mentionsByBlock = {};
      const blockIds = new Set();
      const chapterIds = new Set();
      mentions.forEach(m => {
        if (!mentionsByBlock[m.block_id]) mentionsByBlock[m.block_id] = [];
        mentionsByBlock[m.block_id].push(m);
        blockIds.add(m.block_id);
        const b = blockById[m.block_id];
        if (b?.chapter_id) chapterIds.add(b.chapter_id);
      });

      const speakerBlocks = [];
      const addresseeBlocks = [];
      blocks.forEach(b => {
        if (b.discourse?.speaker_entity_id === entity.entity_id) speakerBlocks.push(b.block_id);
        if (b.discourse?.addressee_entity_id === entity.entity_id) addresseeBlocks.push(b.block_id);
      });

      const summaryChapters = [];
      summaries.forEach(s => {
        if ((s.characters_present || []).includes(entity.entity_id)) {
          summaryChapters.push({
            chapter_id: s.chapter_id,
            title: chapterLabel(s.chapter_id),
          });
        }
      });

      entityLinks[entity.entity_id] = {
        item: entity,
        mentions,
        mentionsByBlock,
        blockIds: [...blockIds],
        chapters: [...chapterIds].map(chapter_id => ({ chapter_id, title: chapterLabel(chapter_id) })),
        speakerBlocks,
        addresseeBlocks,
        summaryChapters,
      };
    });

    const glossaryLinks = {};
    glossary.forEach(term => {
      const occurrences = term.occurrences || [];
      const occurrencesByBlock = {};
      const blockIds = new Set();
      const chapterIds = new Set();
      occurrences.forEach(o => {
        if (!occurrencesByBlock[o.block_id]) occurrencesByBlock[o.block_id] = [];
        occurrencesByBlock[o.block_id].push(o);
        blockIds.add(o.block_id);
        const b = blockById[o.block_id];
        if (b?.chapter_id) chapterIds.add(b.chapter_id);
      });
      glossaryLinks[term.term_id] = {
        item: term,
        occurrences,
        occurrencesByBlock,
        blockIds: [...blockIds],
        chapters: [...chapterIds].map(chapter_id => ({ chapter_id, title: chapterLabel(chapter_id) })),
      };
    });

    return { entities: entityLinks, glossary: glossaryLinks, blockById, chapterById };
  }, [blocks, chapters, glossary, entities, summaries]);

  const activeChapter = useMemo(() => {
    if (!block) return null;
    return chapters.find(ch => ch.chapter_id === block.chapter_id) || null;
  }, [chapters, block]);

  const chapterBlocks = useMemo(() => {
    if (!block) return [];
    const rows = blocks.filter(b => b.chapter_id === block.chapter_id);
    return rows.length ? rows : [block];
  }, [blocks, block]);

  const blockTerms = useMemo(() => block ? (readOnly ? glossary : glossary.filter(t => (t.occurrences || []).some(o => o.block_id === block.block_id))) : [], [glossary, block, readOnly]);
  const blockEntities = useMemo(() => block ? (readOnly ? entities : entities.filter(e => (e.mentions || []).some(m => m.block_id === block.block_id)
    || (block.discourse && [block.discourse.speaker_entity_id, block.discourse.addressee_entity_id].includes(e.entity_id)))) : [], [entities, block, readOnly]);
  const summary = useMemo(() => block ? (summaries.find(s => s.chapter_id === block.chapter_id) || { doc_id: docInfo?.doc_id, chapter_id: block.chapter_id, summary_source: "", source: "" }) : null, [summaries, block, docInfo]);

  const filterCounts = useMemo(() => ({
    unreviewed: blocks.filter(b => !review.blocks?.[b.block_id]?.reviewed).length,
    dialogue: blocks.filter(b => b.block_type === "dialogue").length,
    flag: blocks.filter(b => (b.quality_flags || []).some(f => f !== "ok")).length,
    opening: blocks.filter(b => b.is_chapter_opening).length,
    annotation: blocks.filter(b => annoSet.has(b.block_id)).length,
  }), [blocks, review, annoSet]);

  const visibleBlocks = useMemo(() => blocks.filter(b => {
    if (filters.has("unreviewed") && review.blocks?.[b.block_id]?.reviewed) return false;
    if (filters.has("dialogue") && b.block_type !== "dialogue") return false;
    if (filters.has("flag") && !(b.quality_flags || []).some(f => f !== "ok")) return false;
    if (filters.has("opening") && !b.is_chapter_opening) return false;
    if (filters.has("annotation") && !annoSet.has(b.block_id)) return false;
    return true;
  }), [blocks, filters, review, annoSet]);

  const stats = useMemo(() => {
    const reviewed = blocks.filter(b => review.blocks?.[b.block_id]?.reviewed).length;
    const hardErrors = errors.filter(e => e.severity === "error").length;
    return {
      reviewed,
      totalBlocks: blocks.length,
      glossary: glossary.length,
      glossaryDone: glossary.filter(t => t.status !== "candidate" && t.expected_target).length,
      entities: entities.length,
      entitiesDone: entities.filter(e => e.canonical_target).length,
      summaries: summaries.filter(s => s.summary_source && s.source).length,
      totalChapters: chapters.length,
      refs: references.length,
      refReviewed: references.filter(r => ["reviewed", "locked"].includes(r.status)).length,
      valTotal: Math.max(errors.length, 1),
      valClean: Math.max(errors.length, 1) - hardErrors,
    };
  }, [blocks, glossary, entities, summaries, references, review, errors, chapters]);

  const errorCount = errors.filter(e => e.severity === "error").length;
  const unreviewed = blocks.filter(b => !review.blocks?.[b.block_id]?.reviewed).length;
  const draftRefs = references.filter(r => r.status === "draft").length;
  const needsRetag = Object.values(review.blocks || {}).filter(v => v?.needs_retag).length;
  const missingSummaries = chapters.filter(ch => {
    const s = summaries.find(x => x.chapter_id === ch.chapter_id);
    return !s || !s.summary_source || !s.source;
  }).length;
  const requiredMissing = [];
  ["title", "author", "domain", "genre", "source_format", "license", "contamination_risk"].forEach(k => {
    if (!docInfo?.metadata?.[k]) requiredMissing.push("metadata." + k);
  });
  if (docInfo?.metadata?.extraction_tool !== "manual-synthetic" && !docInfo?.metadata?.raw_sha256) {
    requiredMissing.push("metadata.raw_sha256");
  }
  if (!docInfo?.metadata?.extraction_tool) requiredMissing.push("metadata.extraction_tool");
  if (!docInfo?.metadata?.pipeline_version) requiredMissing.push("metadata.pipeline_version");

  const freezeReasons = [];
  if (errorCount > 0) freezeReasons.push(`${errorCount} validation error${errorCount > 1 ? "s" : ""}`);
  if (unreviewed > 0) freezeReasons.push(`${unreviewed} block${unreviewed > 1 ? "s" : ""} unreviewed`);
  if (draftRefs > 0) freezeReasons.push(`${draftRefs} draft reference${draftRefs > 1 ? "s" : ""}`);
  if (staleCount + needsRetag > 0) freezeReasons.push(`${staleCount + needsRetag} stale span${staleCount + needsRetag > 1 ? "s" : ""}`);
  if (missingSummaries > 0) freezeReasons.push(`${missingSummaries} chapter summar${missingSummaries > 1 ? "ies" : "y"} missing`);
  if (requiredMissing.length > 0) freezeReasons.push(`${requiredMissing.length} required source/provenance field${requiredMissing.length > 1 ? "s" : ""} missing`);
  const freezeReady = freezeReasons.length === 0;

  useEffect(() => {
    if (centerMode !== "preview") setCurrentPreviewRun(null);
  }, [centerMode]);

  const refForBlock = block ? references.find(r => r.block_id === block.block_id) : null;
  const rpCounts = {
    glossary: { text: blockTerms.length || null },
    entities: { text: blockEntities.length || null },
    relations: { text: relations.length || null },
    summary: { text: summary?.summary_source ? null : "empty", tone: summary?.summary_source ? "" : "warn" },
    notes: { text: block?.annotations && (block.annotations.implicit_meaning || block.annotations.narrative_note || block.annotations.tone || (block.annotations.motifs || []).length) ? "set" : null },
    reference: { text: refForBlock?.status || null, tone: refForBlock?.status === "draft" ? "warn" : "" },
    eval_only: { text: (evalOnly.gold_glossary?.length || evalOnly.references?.length) || null, tone: "warn" },
    validate: { text: errorCount || null, tone: errorCount ? "bad" : "" },
    progress: { text: `${stats.reviewed}/${stats.totalBlocks}` },
  };

  function selectBlock(id) { setSelectedId(id); setEditing(false); }
  function toggleFilter(id) { setFilters(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; }); }
  function nextUnreviewedBlock() {
    if (!blocks.length) return;
    const currentIndex = Math.max(0, blocks.findIndex(b => b.block_id === selectedId));
    const after = blocks.slice(currentIndex + 1).find(b => !review.blocks?.[b.block_id]?.reviewed);
    const before = blocks.slice(0, currentIndex + 1).find(b => !review.blocks?.[b.block_id]?.reviewed);
    const next = after || before;
    if (next) {
      selectBlock(next.block_id);
      toast(`Next unreviewed: ${next.block_id}`, "info");
    } else {
      toast("All blocks are reviewed", "good");
    }
  }

  async function selectProject(docId) {
    const chosen = projects.find(p => p.doc_id === docId);
    setActiveDocId(docId);
    localStorage.setItem(STORAGE_DOC, docId);
    if (chosen?.source === "thesis") {
      await loadThesisDataset(thesisJobId(docId));
      setView("workspace");
    } else if (chosen?.status === "available") {
      await loadDataset(docId);
      setView("workspace");
    } else {
      setDocInfo({ doc_id: docId, metadata: {}, provenance: {} });
      setChapters([]);
      setBlocks([]);
      setGlossary([]);
      setEntities([]);
      setRelations([]);
      setSummaries([]);
      setReferences([]);
      setEvalOnly({ gold_glossary: [], references: [] });
      setThesisTranslations({});
      setThesisObservability(null);
      setSelectedCallId(null);
      setSelectedCallDetail(null);
      setReview({ blocks: {}, references: {}, summaries: {} });
      setHistoryState({ can_undo: false, can_redo: false, undo_top: null, redo_top: null, recent: [] });
      setErrors([]);
      setSelectedId(null);
      setView("project");
    }
  }

  function patchDoc(patch) {
    if (readOnly) {
      toast("Read-only thesis view", "info", "Metadata edits belong to a later write lane with explicit audit logs.");
      return;
    }
    if (patch.metadata) {
      const localMetadata = { ...(docInfo?.metadata || {}), ...patch.metadata };
      setDocInfo(d => ({ ...(d || {}), metadata: localMetadata, provenance: d?.provenance || {} }));
      const apiPatch = {};
      Object.entries(patch.metadata).forEach(([k, v]) => {
        if (EDITABLE_META.has(k)) apiPatch[k] = v;
      });
      if (Object.keys(apiPatch).length && activeDocId && blocks.length) {
        queueSave("metadata", () => API.patchMetadata(activeDocId, { ...apiPatch, user: currentUser() }));
      }
    }
  }

  async function createProject(docId, metadata) {
    try {
      const result = await API.createProject({ doc_id: docId, metadata });
      await refreshProjects();
      setDocInfo({ doc_id: result.doc_id, metadata: metadata || {}, provenance: {} });
      setChapters([]);
      setBlocks([]);
      setGlossary([]);
      setEntities([]);
      setRelations([]);
      setSummaries([]);
      setReferences([]);
      setEvalOnly({ gold_glossary: [], references: [] });
      setThesisTranslations({});
      setThesisObservability(null);
      setSelectedCallId(null);
      setSelectedCallDetail(null);
      setReview({ blocks: {}, references: {}, summaries: {} });
      setHistoryState({ can_undo: false, can_redo: false, undo_top: null, redo_top: null, recent: [] });
      setErrors([]);
      setSelectedId(null);
      setActiveDocId(result.doc_id);
      localStorage.setItem(STORAGE_DOC, result.doc_id);
      toast("Project created", "good", result.doc_id);
      return result;
    } catch (err) {
      toast("Create project failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function updateProjectSettings(docId, patch) {
    if (!docId) return null;
    try {
      const result = await API.patchProject(docId, { ...patch, user: currentUser() });
      await refreshProjects();
      toast("Project updated", "good");
      return result;
    } catch (err) {
      toast("Update project failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function deleteProjectById(docId, confirmDocId) {
    if (!docId) return null;
    try {
      const result = await API.deleteProject(docId, { confirm_doc_id: confirmDocId, user: currentUser() });
      const list = await refreshProjects();
      const next = list.find(p => p.status === "available") || list[0];
      if (next) {
        await selectProject(next.doc_id);
      } else {
        setActiveDocId("");
        setDocInfo({ doc_id: "", metadata: {}, provenance: {} });
        setChapters([]);
        setBlocks([]);
        setGlossary([]);
        setEntities([]);
        setRelations([]);
        setSummaries([]);
        setReferences([]);
        setEvalOnly({ gold_glossary: [], references: [] });
        setThesisTranslations({});
        setThesisObservability(null);
        setSelectedCallId(null);
        setSelectedCallDetail(null);
        setView("project");
      }
      toast("Project deleted", "good", result.doc_id);
      return result;
    } catch (err) {
      toast("Delete project failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function uploadSource(file, overwrite) {
    if (!activeDocId || !file) return null;
    try {
      const result = await API.uploadSource(activeDocId, file, overwrite);
      await refreshProjects();
      toast("Source uploaded", "good", result.filename);
      return result;
    } catch (err) {
      toast("Upload failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function runExtract(overwrite) {
    if (!activeDocId) return null;
    try {
      const job = await API.extract(activeDocId, { overwrite: !!overwrite, user: currentUser() });
      await refreshProjects();
      await loadDataset(activeDocId, { silent: true });
      setView("workspace");
      toast("Extraction complete", "good", `${job.document?.blocks || 0} blocks · ${job.document?.chapters || 0} chapters`);
      return job;
    } catch (err) {
      toast("Extraction failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function buildNormalizeCandidate() {
    if (!activeDocId) return null;
    try {
      const candidate = await API.normalizeCandidateParts(activeDocId);
      if (!candidate.paths?.candidate_parts || !candidate.paths?.agent_structure_plan) {
        try {
          const project = await API.getProject(activeDocId);
          const projectRoot = project.root || project.path || "";
          candidate.paths = {
            ...(candidate.paths || {}),
            project_root: projectRoot,
            candidate_parts: joinLocalPath(projectRoot, "working", "normalized", "candidate_parts.json"),
            agent_structure_plan: joinLocalPath(projectRoot, "working", "normalized", "agent_structure_plan.json"),
          };
        } catch (pathErr) {
          candidate.paths = {
            ...(candidate.paths || {}),
            project_root: `ailab_projects/${activeDocId}`,
            candidate_parts: `ailab_projects/${activeDocId}/working/normalized/candidate_parts.json`,
            agent_structure_plan: `ailab_projects/${activeDocId}/working/normalized/agent_structure_plan.json`,
          };
        }
      }
      await refreshProjects();
      toast("Candidate parts built", "good", `${candidate.parts?.length || 0} parts · ${candidate.source_fingerprint || ""}`);
      return candidate;
    } catch (err) {
      toast("Build candidate failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function loadNormalizeAgentPlan() {
    if (!activeDocId) return null;
    try {
      const result = await API.getNormalizeAgentPlan(activeDocId);
      toast("Agent StructurePlan loaded", "good", result.path || "working/normalized/agent_structure_plan.json");
      return result;
    } catch (err) {
      toast("Load agent StructurePlan failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function importNormalizePlan(plan) {
    if (!activeDocId) return null;
    try {
      const result = await API.importStructurePlan(activeDocId, { plan, user: currentUser() });
      const preview = result.preview || {};
      await refreshProjects();
      toast("StructurePlan valid", preview.low_confidence || preview.needs_human_check ? "info" : "good",
        `${preview.chapters?.length || 0} chapters · body ${preview.body_coverage || "n/a"}`);
      return result;
    } catch (err) {
      toast("StructurePlan invalid", "bad", errorMessage(err));
      return {
        ok: false,
        errors: err.errors || [],
        warnings: err.warnings || [],
        validation: err.payload?.data?.validation || null,
        source_fingerprint: err.payload?.data?.source_fingerprint || null,
      };
    }
  }

  async function applyNormalizePlan(options = {}) {
    if (!activeDocId) return null;
    try {
      const job = await API.applyNormalizedStructure(activeDocId, {
        approved: true,
        overwrite: !!options.overwrite,
        force: !!options.force,
        user: currentUser(),
      });
      await refreshProjects();
      await loadDataset(activeDocId, { silent: true });
      setView("workspace");
      toast("Normalized structure applied", "good", `${job.document?.blocks || 0} blocks · ${job.document?.chapters || 0} chapters`);
      return job;
    } catch (err) {
      toast("Apply normalized structure failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function buildAnnotationInput(chapterId) {
    if (!activeDocId || !chapterId) return null;
    try {
      const input = await API.buildAnnotationInput(activeDocId, {
        chapter_id: chapterId,
        user: currentUser(),
      });
      toast("Annotation input built", "good", `${input.blocks?.length || 0} blocks`);
      return input;
    } catch (err) {
      toast("Build annotation input failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function resolveAnnotationCandidate(candidate) {
    if (!activeDocId) return null;
    try {
      const result = await API.resolveAnnotationCandidate(activeDocId, {
        candidate,
        user: currentUser(),
      });
      const okEntities = (result.entities || []).filter(item => item.status === "ok").length;
      const okTerms = (result.glossary || []).filter(item => item.status === "ok").length;
      toast("Annotation candidate resolved", "good", `${okEntities} entities · ${okTerms} terms`);
      return result;
    } catch (err) {
      toast("Resolve annotation candidate failed", "bad", errorMessage(err));
      return {
        ok: false,
        errors: err.errors || [],
        warnings: err.warnings || [],
      };
    }
  }

  async function loadAnnotationAgentCandidate(chapterId) {
    if (!activeDocId || !chapterId) return null;
    try {
      const result = await API.getAnnotationAgentCandidate(activeDocId, chapterId);
      toast("Agent candidate loaded", "good", result.path || chapterId);
      return result;
    } catch (err) {
      toast("Load agent candidate failed", "bad", errorMessage(err));
      return null;
    }
  }

  async function applyAnnotationCandidate(chapterId) {
    if (!activeDocId || !chapterId) return null;
    try {
      const result = await API.applyAnnotationCandidate(activeDocId, {
        chapter_id: chapterId,
        approved: true,
        accept_all_resolved: true,
        user: currentUser(),
      });
      await loadDataset(activeDocId, { silent: true });
      const counts = result.counts || {};
      toast("Annotation candidate applied", "good", `${counts.entities || 0} entities · ${counts.glossary || 0} terms`);
      return result;
    } catch (err) {
      toast("Apply annotation candidate failed", "bad", errorMessage(err));
      return null;
    }
  }

  function findBlock(blockId) {
    return blocks.find(b => b.block_id === blockId) || null;
  }

  function changeType(t, blockId = selectedId) {
    if (readOnly) return toast("Read-only thesis view", "info", "Block type changes are disabled for SQLite read-models.");
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    setBlocks(bs => bs.map(b => b.block_id === target.block_id ? { ...b, block_type: t } : b));
    mutate(() => API.patchBlock(activeDocId, target.block_id, { block_type: t, user: currentUser() }), { success: `block_type -> ${t}` });
  }

  function toggleOpening(blockId = selectedId) {
    if (readOnly) return toast("Read-only thesis view", "info", "Chapter opening changes are disabled for SQLite read-models.");
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    const next = !target.is_chapter_opening;
    setBlocks(bs => bs.map(b => b.block_id === target.block_id ? { ...b, is_chapter_opening: next } : b));
    mutate(() => API.patchBlock(activeDocId, target.block_id, { is_chapter_opening: next, user: currentUser() }));
  }

  function toggleFlag(f, blockId = selectedId) {
    if (readOnly) return toast("Read-only thesis view", "info", "Quality flags are disabled for SQLite read-models.");
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    let flags;
    const current = target.quality_flags || ["ok"];
    if (f === "ok") flags = ["ok"];
    else {
      flags = current.includes(f) ? current.filter(x => x !== f) : [...current.filter(x => x !== "ok"), f];
      if (!flags.length) flags = ["ok"];
    }
    setBlocks(bs => bs.map(b => b.block_id === target.block_id ? { ...b, quality_flags: flags } : b));
    mutate(() => API.patchBlock(activeDocId, target.block_id, { quality_flags: flags, user: currentUser() }));
  }

  function markReviewed(blockId = selectedId) {
    if (readOnly) return toast("Read-only thesis view", "info", "Review writes are disabled for SQLite read-models.");
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    const next = !review.blocks?.[target.block_id]?.reviewed;
    mutate(() => API.patchReview(activeDocId, target.block_id, { reviewed: next, reviewed_by: currentUser(), user: currentUser() }), {
      success: next ? `${target.block_id} marked reviewed` : `${target.block_id} marked unreviewed`,
    });
  }

  async function commitClean(blockId, text) {
    if (readOnly) return toast("Read-only thesis view", "info", "Clean text edits are disabled for SQLite read-models.");
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    setBlocks(bs => bs.map(b => b.block_id === target.block_id ? { ...b, clean_text: text } : b));
    setEditing(false);
    const result = await mutate(() => API.patchBlock(activeDocId, target.block_id, { clean_text: text, user: currentUser() }), { refresh: true });
    const broke = result?.stale_spans?.length || 0;
    const relocated = result?.relocated_count || 0;
    if (broke > 0 && relocated > 0) {
      toast(`Clean text saved · ${relocated} span${relocated > 1 ? "s" : ""} auto-relocated, ${broke} need re-tag`, "bad", "Re-tag only the highlighted stale span(s).");
    } else if (broke > 0) {
      toast(`Clean text saved · ${broke} annotation span${broke > 1 ? "s" : ""} no longer match`, "bad", "Re-tag from the right panel to clear the warning.");
    } else if (relocated > 0) {
      toast(`Clean text saved · ${relocated} span${relocated > 1 ? "s" : ""} auto-relocated`, "good");
    } else {
      toast("Clean text saved", "good");
    }
  }

  async function addGlossary(blockId, sel) {
    if (readOnly) return toast("Read-only thesis view", "info", "Glossary writes are disabled for SQLite read-models.");
    const target = findBlock(blockId);
    if (!target || !sel) return;
    setSelectedId(target.block_id);
    openRightTab("glossary");
    const result = await mutate(() => API.addGlossary(activeDocId, {
      block_id: target.block_id,
      start: sel.start,
      end: sel.end,
      source_term: sel.text.trim(),
      user: currentUser(),
    }), { success: `Added glossary occurrence "${sel.text.trim()}"`, fail: "Add glossary failed" });
    if (result) toast("Set expected_target in the Glossary tab.", "info");
  }
  async function addEntity(blockId, sel) {
    if (readOnly) return toast("Read-only thesis view", "info", "Entity writes are disabled for SQLite read-models.");
    const target = findBlock(blockId);
    if (!target || !sel) return;
    setSelectedId(target.block_id);
    openRightTab("entities");
    const result = await mutate(() => API.addEntity(activeDocId, {
      block_id: target.block_id,
      start: sel.start,
      end: sel.end,
      surface: sel.text.trim(),
      user: currentUser(),
    }), { success: `Added entity mention "${sel.text.trim()}"`, fail: "Add entity failed" });
    if (result) toast("Set canonical_target and pronoun policy.", "info");
  }

  function updateTerm(termId, patch) {
    if (readOnly) return toast("Read-only thesis view", "info", "Runtime memory is immutable in APP-A01.");
    setGlossary(gs => gs.map(t => t.term_id === termId ? { ...t, ...patch } : t));
    queueSave(`term:${termId}:${Object.keys(patch).join(",")}`, () => API.patchGlossary(activeDocId, termId, { ...patch, user: currentUser() }));
  }
  function updateEntity(entityId, patch) {
    if (readOnly) return toast("Read-only thesis view", "info", "Runtime memory is immutable in APP-A01.");
    setEntities(es => es.map(e => e.entity_id === entityId ? { ...e, ...patch } : e));
    queueSave(`entity:${entityId}:${Object.keys(patch).join(",")}`, () => API.patchEntity(activeDocId, entityId, { ...patch, user: currentUser() }));
  }
  async function createRelation(payload) {
    if (readOnly) return toast("Read-only thesis view", "info", "Relation writes are disabled for SQLite read-models.");
    if (!activeDocId) return null;
    openRightTab("relations");
    const result = await mutate(() => API.createRelation(activeDocId, { ...payload, user: currentUser() }), {
      success: "Relation added",
      fail: "Add relation failed",
    });
    return result;
  }
  function updateRelation(relationId, patch) {
    if (readOnly) return toast("Read-only thesis view", "info", "Relation writes are disabled for SQLite read-models.");
    const { relation_id, doc_id, ...apiPatch } = patch || {};
    setRelations(rs => rs.map(r => r.relation_id === relationId ? { ...r, ...apiPatch } : r));
    const fields = Object.keys(apiPatch).join(",") || "save";
    queueSave(`relation:${relationId}:${fields}`, () => API.patchRelation(activeDocId, relationId, { ...apiPatch, user: currentUser() }));
  }
  function updateSummary(chapterId, patch) {
    if (readOnly) return toast("Read-only thesis view", "info", "Summary writes are disabled for SQLite read-models.");
    if (!chapterId) return;
    setSummaries(ss => {
      const exists = ss.some(s => s.chapter_id === chapterId);
      if (exists) return ss.map(s => s.chapter_id === chapterId ? { ...s, ...patch } : s);
      return [...ss, { doc_id: docInfo?.doc_id, chapter_id: chapterId, ...patch }];
    });
    queueSave(`summary:${chapterId}:${Object.keys(patch).join(",")}`, () => API.patchSummary(activeDocId, chapterId, { ...patch, user: currentUser() }));
  }
  function updateBlockNotes(blockId, patch) {
    if (readOnly) return toast("Read-only thesis view", "info", "Block notes are disabled for SQLite read-models.");
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    setBlocks(bs => bs.map(b => b.block_id === target.block_id ? {
      ...b,
      annotations: { ...(b.annotations || {}), ...patch },
    } : b));
    queueSave(`block-notes:${blockId}:${Object.keys(patch).join(",")}`, () => API.patchBlockNotes(activeDocId, blockId, { ...patch, user: currentUser() }));
  }
  function updateReference(referenceId, patch) {
    if (readOnly) return toast("Read-only thesis view", "info", "Reference writes are disabled for SQLite read-models.");
    setReferences(rs => rs.map(r => r.reference_id === referenceId ? { ...r, ...patch, canonical: false } : r));
  }
  function updateDiscourse(patch) {
    if (readOnly) return toast("Read-only thesis view", "info", "Discourse writes are disabled for SQLite read-models.");
    if (!block) return;
    const discourse = { ...(block.discourse || {}), ...patch };
    setBlocks(bs => bs.map(b => b.block_id === block.block_id ? { ...b, discourse } : b));
    mutate(() => API.patchBlock(activeDocId, block.block_id, { discourse, user: currentUser() }), { success: "Discourse saved" });
  }
  function saveDraft(referenceId) {
    const r = references.find(x => x.reference_id === referenceId);
    if (!r) return;
    mutate(() => API.saveReferenceDraft(activeDocId, {
      reference_id: r.reference_id,
      block_id: r.block_id,
      draft_vi: r.reference_vi || r.draft_vi || "",
      reference_vi: r.reference_vi || "",
      source: r.source || "",
      translated_by: r.translated_by || currentUser(),
      ai_model: r.ai_model || "",
      prompt_id: r.prompt_id || "",
      notes: r.notes || "",
      user: currentUser(),
    }), { success: "Reference draft saved" });
  }
  function createReferenceDraft(blockId, payload) {
    mutate(() => API.saveReferenceDraft(activeDocId, {
      block_id: blockId,
      draft_vi: payload.reference_vi || "",
      reference_vi: payload.reference_vi || "",
      source: payload.source || "human",
      translated_by: currentUser(),
      ai_model: payload.ai_model || "",
      user: currentUser(),
    }), { success: "Reference draft saved", fail: "Reference draft failed" });
  }
  function markReviewedReference(referenceId) {
    const r = references.find(x => x.reference_id === referenceId);
    if (!r) return;
    mutate(() => API.reviewReference(activeDocId, referenceId, {
      reference_vi: r.reference_vi || r.draft_vi || "",
      source: r.source || "",
      reviewed_by: currentUser(),
      ai_model: r.ai_model || "",
      prompt_id: r.prompt_id || "",
      user: currentUser(),
    }), { success: "Reference marked reviewed", fail: "Reference cannot be reviewed" });
  }
  function lockReference(referenceId) {
    mutate(() => API.lockReference(activeDocId, referenceId, { user: currentUser() }), { success: "Reference locked", fail: "Only reviewed references can be locked" });
  }

  function deleteTerm(t) {
    setModal({ kind: "delete-term", term: t });
  }

  function deleteEntity(e) {
    setModal({ kind: "delete-entity", entity: e });
  }

  function deleteRelation(r) {
    setModal({ kind: "delete-relation", relation: r });
  }

  async function confirmDeleteTerm() {
    const term = modal?.term;
    if (!term) return;
    touchStart();
    try {
      const result = await API.deleteGlossary(activeDocId, term.term_id, { user: currentUser() });
      await loadDataset(activeDocId, { silent: true });
      touchDone();
      setModal(null);
      toast(`Deleted term "${term.source_term}"`, "good", `${result.removed_occurrences || 0} occurrence(s) removed`);
    } catch (err) {
      touchDone();
      const first = firstError(err);
      setModal({ kind: "delete-blocked", title: "Cannot delete term", message: errorMessage(err), references: first.references || [] });
      toast("Cannot delete term", "bad", errorMessage(err));
    }
  }

  async function confirmDeleteEntity() {
    const entity = modal?.entity;
    if (!entity) return;
    touchStart();
    try {
      const result = await API.deleteEntity(activeDocId, entity.entity_id, { user: currentUser() });
      await loadDataset(activeDocId, { silent: true });
      touchDone();
      setModal(null);
      toast(`Deleted entity "${entity.canonical_source || entity.entity_id}"`, "good", `${result.removed_mentions || 0} mention(s) removed`);
    } catch (err) {
      touchDone();
      const first = firstError(err);
      setModal({ kind: "delete-blocked", title: "Cannot delete entity", message: errorMessage(err), references: first.references || [] });
      toast("Cannot delete entity", "bad", errorMessage(err));
    }
  }

  async function confirmDeleteRelation() {
    const relation = modal?.relation;
    if (!relation) return;
    touchStart();
    try {
      await API.deleteRelation(activeDocId, relation.relation_id, { user: currentUser() });
      await loadDataset(activeDocId, { silent: true });
      touchDone();
      setModal(null);
      toast(`Deleted relation ${relation.relation_id}`, "good");
    } catch (err) {
      touchDone();
      setModal({ kind: "delete-blocked", title: "Cannot delete relation", message: errorMessage(err), references: [] });
      toast("Cannot delete relation", "bad", errorMessage(err));
    }
  }

  async function runValidate() {
    if (readOnly) return toast("Read-only thesis view", "info", "Validation is for AI-LAB JSONL workspaces; scoring/reporting is APP-D01.");
    openRightTab("validate");
    try {
      const report = await API.validate(activeDocId, { user: currentUser() });
      const items = normalizeErrors(report);
      setErrors(items);
      toast(`Validation: ${report.errors?.length || 0} error${(report.errors?.length || 0) !== 1 ? "s" : ""}, ${report.warnings?.length || 0} warning`, report.ok ? "good" : "bad");
    } catch (err) {
      setErrors((err.errors || []).map(e => ({ severity: "error", ...e })));
      toast("Validation failed", "bad", errorMessage(err));
    }
  }

  async function migrateSchema() {
    if (readOnly) return toast("Read-only thesis view", "info", "Schema migration is disabled for SQLite read-models.");
    if (!activeDocId || schemaMigrating) return;
    openRightTab("validate");
    setSchemaMigrating(true);
    touchStart();
    try {
      const result = await API.migrateSchema(activeDocId, { user: currentUser() });
      await loadDataset(activeDocId, { silent: true });
      if (result.validation) setErrors(normalizeErrors(result.validation));
      touchDone();
      const ok = result.validation?.ok !== false;
      const actions = (result.actions || []).join(" · ");
      toast(ok ? "Schema migrated to 1.5" : "Schema migrated with validation issues", ok ? "good" : "bad", actions || "Project files updated.");
    } catch (err) {
      setErrors((err.errors || []).map(e => ({ severity: "error", ...e })));
      touchDone();
      toast("Schema migration failed", "bad", errorMessage(err));
    } finally {
      setSchemaMigrating(false);
    }
  }

  function jumpTo(e) { if (e.block_id) { selectBlock(e.block_id); toast(`Jumped to ${e.block_id}`, "info"); } }
  function buildQcExport() {
    return {
      kind: "qc_report",
      doc_id: docInfo?.doc_id || activeDocId,
      generated_at: new Date().toISOString(),
      schema_version: docInfo?.schema_version || "",
      metadata: docInfo?.metadata || {},
      counts: {
        chapters: chapters.length,
        blocks: blocks.length,
        glossary_terms: glossary.length,
        entities: entities.length,
        entity_relations: relations.length,
        chapter_summaries: summaries.length,
        references: references.length,
        reviewed_references: references.filter(r => ["reviewed", "locked"].includes(r.status)).length,
      },
      review: {
        reviewed_blocks: stats.reviewed,
        unreviewed_blocks: unreviewed,
        blocks_needing_retag: needsRetag,
        stale_spans: staleCount,
        missing_chapter_summaries: missingSummaries,
        draft_references: draftRefs,
      },
      validation: {
        error_count: errorCount,
        warning_count: errors.filter(e => e.severity === "warning").length,
        items: errors,
      },
      freeze: {
        ready: freezeReady,
        reasons: freezeReasons,
        required_missing: requiredMissing,
      },
    };
  }
  function exportQcReport() {
    const report = buildQcExport();
    downloadAppJsonFile(`${safeFilePart(report.doc_id)}_qc_report.json`, report);
    toast("QC report exported", "good", `${safeFilePart(report.doc_id)}_qc_report.json`);
  }
  function exportPreviewRun() {
    const run = currentPreviewRun?.run;
    if (!run) {
      toast("No preview run loaded", "bad", "Open Preview and select a run first.");
      return;
    }
    const chapterId = run.chapter_id || currentPreviewRun.chapter_id || "chapter";
    const payload = {
      kind: "translation_preview_export",
      exported_at: new Date().toISOString(),
      notice: "Preview only. This is not gold and must not be promoted into manual_reference_subset.",
      run,
    };
    const filename = `${safeFilePart(run.doc_id || docInfo?.doc_id)}_${safeFilePart(chapterId)}_${safeFilePart(run.run_id)}_preview.json`;
    downloadAppJsonFile(filename, payload);
    toast("Translation preview exported", "good", filename);
  }
  async function exportDatasetPackage() {
    const suggestedName = `${safeFilePart(docInfo?.doc_id || activeDocId)}_dataset_package.zip`;
    const saveHandle = await pickZipSaveHandle(suggestedName);
    if (saveHandle === "aborted") {
      toast("Export cancelled", "info");
      return;
    }
    touchStart();
    try {
      const result = await API.exportProject(activeDocId, { user: currentUser() });
      const filename = result.filename || String(result.zip || suggestedName).split(/[\\/]/).pop() || suggestedName;
      const blob = await API.downloadExport(activeDocId, filename);
      if (saveHandle) {
        await writeBlobToHandle(saveHandle, blob);
      } else {
        downloadBlobFile(filename, blob);
      }
      touchDone();
      toast("Exported dataset package", "good", `${filename} Â· saved in project exports Â· QC included`);
    } catch (err) {
      touchDone();
      toast("Dataset package export failed", "bad", errorMessage(err));
    }
  }
  async function exportDatasetWithPreviews() {
    const suggestedName = `${safeFilePart(docInfo?.doc_id || activeDocId)}_dataset_plus_previews.zip`;
    const saveHandle = await pickZipSaveHandle(suggestedName);
    if (saveHandle === "aborted") {
      toast("Export cancelled", "info");
      return;
    }
    touchStart();
    try {
      const result = await API.exportProjectWithPreviews(activeDocId, { user: currentUser() });
      const filename = result.filename || String(result.zip || suggestedName).split(/[\\/]/).pop() || suggestedName;
      const blob = await API.downloadExport(activeDocId, filename);
      if (saveHandle) {
        await writeBlobToHandle(saveHandle, blob);
      } else {
        downloadBlobFile(filename, blob);
      }
      touchDone();
      const counts = result.manifest_data?.translation_preview?.counts || {};
      toast(
        "Exported dataset + previews",
        "good",
        `${filename} · saved in project exports · runs ${counts.runs || 0}, inputs ${counts.inputs || 0}`
      );
    } catch (err) {
      touchDone();
      toast("Dataset + preview export failed", "bad", errorMessage(err));
    }
  }
  function doExport(kind = "package") {
    if (readOnly && kind !== "qc") {
      return toast("Read-only thesis view", "info", "Dataset package export stays on the AI-LAB lane; use QC export for this read-model.");
    }
    if (kind === "qc") return exportQcReport();
    if (kind === "preview") return exportPreviewRun();
    if (kind === "dataset-previews") return exportDatasetWithPreviews();
    return exportDatasetPackage();
  }
  function doFreeze() {
    if (readOnly) return toast("Read-only thesis view", "info", "Freeze is disabled for pipeline SQLite read-models.");
    setModal({ kind: "freeze" });
  }

  async function runUndo() {
    if (!historyState.can_undo || dirty) return;
    const result = await mutate(() => API.undo(activeDocId, { user: currentUser() }), { refresh: false, fail: "Undo failed" });
    if (!result) return;
    await loadDataset(activeDocId, { silent: true });
    const target = result.event?.target || {};
    if (target.block_id) setSelectedId(target.block_id);
    toast(`Undo: ${result.event?.label || "last change"}`, "good");
  }

  async function runRedo() {
    if (!historyState.can_redo || dirty) return;
    const result = await mutate(() => API.redo(activeDocId, { user: currentUser() }), { refresh: false, fail: "Redo failed" });
    if (!result) return;
    await loadDataset(activeDocId, { silent: true });
    const target = result.event?.target || {};
    if (target.block_id) setSelectedId(target.block_id);
    toast(`Redo: ${result.event?.label || "last undone change"}`, "good");
  }

  function isNativeTextUndoTarget(target) {
    if (!target) return false;
    const tag = (target.tagName || "").toLowerCase();
    return tag === "textarea" || tag === "input" || target.isContentEditable;
  }

  async function confirmExport() {
    const result = await mutate(() => API.exportProject(activeDocId, { user: currentUser() }), { refresh: false });
    if (result) {
      setModal(null);
      toast("Exported current package", "good", result.zip || result.path || "Export created.");
    }
  }
  async function confirmFreeze() {
    try {
      const result = await API.freezeProject(activeDocId, { user: currentUser() });
      setModal(null);
      await loadDataset(activeDocId, { silent: true });
      toast("Dataset snapshot frozen", "good", `${result.version || "versioned"} · ${result.zip || ""}`);
    } catch (err) {
      const first = err.errors?.[0] || {};
      setModal({ kind: "freeze", serverReasons: first.reasons || [errorMessage(err)] });
      toast("Freeze blocked", "bad", (first.reasons || []).join("; ") || errorMessage(err));
    }
  }

  useEffect(() => {
    function onKey(ev) {
      const mod = ev.metaKey || ev.ctrlKey;
      if (mod && !isNativeTextUndoTarget(ev.target)) {
        const key = ev.key.toLowerCase();
        if (key === "z" && !ev.shiftKey) {
          ev.preventDefault();
          runUndo();
          return;
        }
        if (key === "y" || (key === "z" && ev.shiftKey)) {
          ev.preventDefault();
          runRedo();
          return;
        }
      }
      if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter" && !editing) { ev.preventDefault(); markReviewed(); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (loading) {
    return <StartupState title="Loading backend dataset" message={`Connecting to ${API.baseUrl}...`} />;
  }
  if (bootError) {
    return (
      <>
        <StartupState title="Backend offline" message="The UI could not reach the Flask backend. Start the backend on port 5000, then retry." secondary={bootError} action="Retry" onAction={boot} />
        <Toasts items={toasts} onDismiss={dismiss} />
      </>
    );
  }

  if (view === "project") {
    return (
      <>
        <ProjectSourceScreen
          projects={projects}
          activeDocId={activeDocId}
          docInfo={docInfo || { doc_id: activeDocId, metadata: {}, provenance: {} }}
          chapters={chapters}
          blocks={blocks}
          errors={errors}
          onSelectProject={selectProject}
          onCreateProject={createProject}
          onPatchDoc={patchDoc}
          onUpdateProject={updateProjectSettings}
          onDeleteProject={deleteProjectById}
          onUploadSource={uploadSource}
          onBack={() => setView("workspace")}
          onExtract={runExtract}
          onBuildNormalizeCandidate={buildNormalizeCandidate}
          onLoadNormalizeAgentPlan={loadNormalizeAgentPlan}
          onImportNormalizePlan={importNormalizePlan}
          onApplyNormalizePlan={applyNormalizePlan}
          onBuildAnnotationInput={buildAnnotationInput}
          onLoadAnnotationAgentCandidate={loadAnnotationAgentCandidate}
          onResolveAnnotationCandidate={resolveAnnotationCandidate}
          onApplyAnnotationCandidate={applyAnnotationCandidate}
          readOnly={readOnly}
        />
        <Toasts items={toasts} onDismiss={dismiss} />
      </>
    );
  }

  if (!block || !docInfo) {
    return (
      <>
        <StartupState title="No extracted document" message="Open Project / Source, upload a TXT or EPUB file, then run Extract." action="Open Project / Source" onAction={() => setView("project")} />
        <Toasts items={toasts} onDismiss={dismiss} />
      </>
    );
  }

  return (
    <div className="app">
      <TopBar docId={docInfo.doc_id} dirty={dirty} lastSaved={lastSaved}
        onValidate={runValidate} onExportOption={doExport} onFreeze={doFreeze}
        onUndo={runUndo} onRedo={runRedo} history={historyState}
        freezeReady={freezeReady} freezeReasons={freezeReasons} previewReadOnly={centerMode === "preview" || readOnly} canExportPreview={!!currentPreviewRun?.run} />
      <div className="workspace">
        <LeftSidebar docInfo={docInfo} projects={projects} blocks={visibleBlocks} chapters={chapters} review={review}
          annoSet={annoSet} selectedId={selectedId} onSelect={selectBlock}
          onSelectProject={selectProject}
          filters={filters} onToggleFilter={toggleFilter} counts={filterCounts} total={blocks.length}
          errors={errors}
          onOpenProjectSource={() => setView("project")} />
        <CenterEditor block={block} docInfo={docInfo} reviewed={!!review.blocks?.[selectedId]?.reviewed} spans={spans}
          editing={editing} mode={centerMode} onModeChange={setCenterMode}
          chapter={activeChapter} chapters={chapters} chapterBlocks={chapterBlocks} allBlocks={blocks} review={review} selectedId={selectedId}
          getSpansForBlock={getSpansForBlock} linkIndex={linkIndex} onSelectBlock={selectBlock} onNextUnreviewed={nextUnreviewedBlock}
          onEdit={() => setEditing(true)} onCommitClean={commitClean} onCancelEdit={() => setEditing(false)}
          onChangeType={changeType} onToggleOpening={() => toggleOpening(selectedId)} onToggleFlag={(flag) => toggleFlag(flag, selectedId)} onMarkReviewed={markReviewed}
          onAddGlossary={addGlossary} onAddEntity={addEntity} onPreviewRunChange={setCurrentPreviewRun} readOnly={readOnly}
          observability={thesisObservability}
          selectedCallId={selectedCallId}
          selectedCallDetail={selectedCallDetail}
          callDetailLoading={callDetailLoading}
          onSelectCall={setSelectedCallId} />
        {centerMode === "preview" ? (
          <PreviewRightPanel docInfo={docInfo} block={block} />
        ) : (
          <RightPanel openTabs={rightOpenTabs} onToggleTab={toggleRightTab} counts={rpCounts}
            ctx={{ docInfo, terms: blockTerms, entities: blockEntities, allEntities: entities, relations, block, summary, references, evalOnly, thesisTranslations, readOnly, errors, stats, freezeReasons,
              schemaMigrating, onMigrateSchema: migrateSchema,
              onDeleteTerm: deleteTerm, onDeleteEntity: deleteEntity, onUpdateTerm: updateTerm, onUpdateEntity: updateEntity, onUpdateSummary: updateSummary,
              onCreateRelation: createRelation, onUpdateRelation: updateRelation, onDeleteRelation: deleteRelation,
              onUpdateBlockNotes: updateBlockNotes,
              onUpdateReference: updateReference, onCreateReference: createReferenceDraft, onSaveDraft: saveDraft, onMarkReviewedReference: markReviewedReference,
              onLockReference: lockReference, onUpdateDiscourse: updateDiscourse, onJump: jumpTo, history: historyState }} />
        )}
      </div>

      <Toasts items={toasts} onDismiss={dismiss} />

      {modal?.kind === "export" && (
        <Modal title="Export dataset" icon={Ic.upload} onClose={() => setModal(null)}
          actions={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn primary" onClick={confirmExport}>Export package</button></>}>
          <p>Exports the current dataset package. It may still be a working package if validation or review gates are not clear.</p>
          <ul className="file-list">
            {["document.json","glossary.jsonl","entities.jsonl","entity_relations.jsonl","chapter_summaries.jsonl","manual_reference_subset.jsonl"].map(f =>
              <li key={f}><Ic.file size={12} /><span className="mono">{f}</span></li>)}
          </ul>
          <p className="muted">{errorCount > 0 ? <><Ic.alert size={11} /> {errorCount} validation error(s) present.</> : "No validation errors in the current report."}</p>
        </Modal>
      )}

      {modal?.kind === "freeze" && (
        <Modal title="Freeze dataset" icon={Ic.snow} onClose={() => setModal(null)}
          actions={<><button className="btn" onClick={() => setModal(null)}>Close</button>
            <button className="btn primary" disabled={!freezeReady && !modal.serverReasons} onClick={confirmFreeze}><Ic.snow size={12} />Freeze snapshot</button></>}>
          <p>Freeze creates a validated, versioned snapshot. It is blocked until validation, review, references, spans, summaries, and provenance gates are clear.</p>
          <div className="freeze-checks">
            {(modal.serverReasons || freezeReasons).length ? (modal.serverReasons || freezeReasons).map(reason => (
              <div key={reason} className="fc-row bad"><Ic.xCircle size={13} />{reason}</div>
            )) : <div className="fc-row ok"><Ic.checkCircle size={13} />All freeze gates are clear.</div>}
          </div>
        </Modal>
      )}

      {modal?.kind === "delete-term" && (() => {
        const term = modal.term;
        const locked = ["locked", "human_verified"].includes(term?.status);
        const occCount = (term?.occurrences || []).length;
        const blockCount = new Set((term?.occurrences || []).map(o => o.block_id)).size;
        return (
          <Modal title="Delete glossary term" icon={Ic.trash} tone="bad" onClose={() => setModal(null)}
            actions={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
              <button className="btn danger" disabled={locked} onClick={confirmDeleteTerm}>Delete term</button></>}>
            {locked ? (
              <p><b>{term.source_term}</b> is {term.status}. Unlock or downgrade it before deleting.</p>
            ) : (
              <>
                <p>Delete <b>{term.source_term}</b> and remove its annotation footprint from the document?</p>
                <p className="muted">{occCount} occurrence(s) across {blockCount || 0} block(s) will be removed. This is undoable.</p>
              </>
            )}
          </Modal>
        );
      })()}

      {modal?.kind === "delete-entity" && (() => {
        const entity = modal.entity;
        const mentionCount = (entity?.mentions || []).length;
        const blockCount = new Set((entity?.mentions || []).map(m => m.block_id)).size;
        return (
          <Modal title="Delete entity" icon={Ic.trash} tone="bad" onClose={() => setModal(null)}
            actions={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
              <button className="btn danger" onClick={confirmDeleteEntity}>Delete entity</button></>}>
            <p>Delete <b>{entity.canonical_source || entity.entity_id}</b> and remove its own mention footprint from the document?</p>
            <p className="muted">{mentionCount} mention(s) across {blockCount || 0} block(s) will be removed. This is undoable.</p>
            <p className="muted">If this entity is used by discourse, chapter summary, or relation, deletion will be blocked and those references must be removed first.</p>
          </Modal>
        );
      })()}

      {modal?.kind === "delete-relation" && (() => {
        const relation = modal.relation;
        return (
          <Modal title="Delete entity relation" icon={Ic.trash} tone="bad" onClose={() => setModal(null)}
            actions={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
              <button className="btn danger" onClick={confirmDeleteRelation}>Delete relation</button></>}>
            <p>Delete relation <b>{relation.relation_id}</b>?</p>
            <p className="muted">This removes only the document-level address-policy relation. Entity mentions, glossary, discourse, and summaries are kept. This is undoable.</p>
          </Modal>
        );
      })()}

      {modal?.kind === "delete-blocked" && (
        <Modal title={modal.title || "Delete blocked"} icon={Ic.alert} tone="bad" onClose={() => setModal(null)}
          actions={<button className="btn" onClick={() => setModal(null)}>Close</button>}>
          <p>{modal.message || "This item is still referenced."}</p>
          {(modal.references || []).length ? (
            <ul className="file-list">
              {modal.references.map((ref, index) => <li key={index}><Ic.link size={12} /><span className="mono">{describeExternalRef(ref)}</span></li>)}
            </ul>
          ) : <p className="muted">Remove related references first, then try again.</p>}
        </Modal>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
