/* ===== APP: real backend wiring, state adapter, interactions ===== */
const { useState, useEffect, useMemo, useRef, useCallback } = React;

const API = window.AILAB_API;
const UI_VERSION = "0.7.0";
const STORAGE_DOC = "ailab.doc_id";
const STORAGE_USER = "ailab.user";
const STORAGE_CENTER_MODE = "ailab.center_mode";
const STORAGE_LEFT_PANEL = "thesis.left_panel_open";
const STORAGE_RIGHT_PANEL = "thesis.right_panel_open";
const STORAGE_RIGHT_PANEL_EXPANDED = "thesis.right_panel_expanded";
const THESIS_PREFIX = "thesis:";
const PROJECT_ROUTE_HASH = "#project";
const CONSOLE_ROUTE_HASH = "#console";
const REPORT_ROUTE_HASH = "#report";
const DEFAULT_USER = "U2 · Mai";
const EDITABLE_META = new Set(["title", "author", "domain", "genre", "source_format", "license", "source_url", "contamination_risk"]);
const RUN_TERMINAL_STATUSES = new Set(["done", "failed", "cancelled", "canceled", "error"]);

function isRunTerminalStatus(status) {
  return RUN_TERMINAL_STATUSES.has(String(status || "").toLowerCase());
}

function emptyRunEventAggregate() {
  return {
    total_events: 0,
    cost_total: 0,
    cache_hits: 0,
    cache_known: 0,
    llm_events: 0,
    warning_count: 0,
    error_count: 0,
    stages: {},
    agents: [],
    severities: [],
    latest_artifact: null,
    latest_block: null,
    last_ts: null,
  };
}

function aggregateEventType(event) {
  return String(event?.event_type || event?.event || "");
}

function aggregatePayload(event) {
  return event?.payload || {};
}

function updateRunEventAggregate(prevAggregate, events) {
  const aggregate = {
    ...emptyRunEventAggregate(),
    ...(prevAggregate || {}),
    stages: { ...(prevAggregate?.stages || {}) },
    agents: [...(prevAggregate?.agents || [])],
    severities: [...(prevAggregate?.severities || [])],
  };
  const agents = new Set(aggregate.agents);
  const severities = new Set(aggregate.severities);
  (events || []).forEach(event => {
    const payload = aggregatePayload(event);
    const eventType = aggregateEventType(event);
    const stage = String(event?.stage || payload.stage || "");
    const severity = String(event?.severity || payload.severity || "info");
    const agent = String(event?.agent || payload.agent || "");
    aggregate.total_events += 1;
    aggregate.cost_total += Number(payload.cost_delta_usd || payload.cost_usd || event?.cost_usd || 0) || 0;
    if (eventType === "llm_call" || eventType === "response_received") aggregate.llm_events += 1;
    if (payload.cache_hit !== undefined || payload.from_cache !== undefined) {
      aggregate.cache_known += 1;
      if (payload.cache_hit === true || payload.from_cache === true) aggregate.cache_hits += 1;
    }
    if (severity === "warning") aggregate.warning_count += 1;
    if (severity === "error") aggregate.error_count += 1;
    if (agent) agents.add(agent);
    if (severity) severities.add(severity);
    if (payload.artifact_path) aggregate.latest_artifact = { stage, agent, event_type: eventType, payload, ts: event?.ts || "" };
    if (payload.block_id || payload.block_ids?.length) aggregate.latest_block = { stage, agent, event_type: eventType, payload, ts: event?.ts || "" };
    if (event?.ts) aggregate.last_ts = event.ts;
    if (stage) {
      const prior = aggregate.stages[stage] || { count: 0, done: 0, total: 0, total_known: false, last_event_type: "", last_ts: "" };
      const progress = payload.progress || {};
      const hasDone = progress.done !== undefined || payload.done !== undefined;
      const hasTotal = progress.total !== undefined || payload.total !== undefined;
      const done = Number(hasDone ? (progress.done ?? payload.done) : (prior.done ?? 0));
      const total = Number(hasTotal ? (progress.total ?? payload.total) : (prior.total ?? 0));
      aggregate.stages[stage] = {
        count: prior.count + 1,
        done: Number.isFinite(done) ? Math.max(prior.done || 0, done) : prior.done || 0,
        total: Number.isFinite(total) ? Math.max(prior.total || 0, total) : prior.total || 0,
        total_known: prior.total_known || progress.total_known === true || payload.total_known === true || hasTotal,
        last_event_type: eventType,
        last_ts: event?.ts || prior.last_ts || "",
      };
    }
  });
  aggregate.agents = Array.from(agents).sort();
  aggregate.severities = Array.from(severities).sort();
  return aggregate;
}

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

function isThesisDatasetId(docId) {
  return String(docId || "").startsWith(THESIS_PREFIX);
}

function viewFromLocation() {
  if (window.location.hash === PROJECT_ROUTE_HASH) return "project";
  if (window.location.hash === CONSOLE_ROUTE_HASH) return "console";
  if (window.location.hash === REPORT_ROUTE_HASH) return "report";
  return "workspace";
}

function firstError(err) {
  return err?.errors?.[0] || err?.payload?.errors?.[0] || {};
}

function urlParam(names) {
  const params = new URLSearchParams(window.location.search || "");
  for (const name of names) {
    const value = params.get(name);
    if (value) return value;
  }
  return "";
}

function thesisScopedStoredParam(jobId, names, suffix) {
  const value = urlParam(names);
  if (!jobId) return "";
  const storageKey = `ailab.thesis.${jobId}.${suffix}`;
  if (value) {
    localStorage.setItem(storageKey, value);
    return value;
  }
  return localStorage.getItem(storageKey) || "";
}

function thesisRuntimeParams(jobId, { includeCascade = false, project = null } = {}) {
  const result = {};
  const experimentId = thesisScopedStoredParam(jobId, ["experiment_id", "thesis_experiment_id"], "experiment_id") || project?.experiment_id || "";
  const stage = thesisScopedStoredParam(jobId, ["stage", "thesis_stage"], "stage");
  const cascadeReport = thesisScopedStoredParam(jobId, ["cascade_report", "thesis_cascade_report"], "cascade_report");
  if (experimentId) result.experiment_id = experimentId;
  if (stage) result.stage = stage;
  if (includeCascade && cascadeReport) result.cascade_report = cascadeReport;
  return result;
}

function cssAttrEscape(value) {
  const raw = String(value || "");
  if (window.CSS?.escape) return window.CSS.escape(raw);
  return raw.replace(/\\/g, "\\\\").replace(/"/g, "\\\"");
}

function splitWords(value) {
  return String(value || "")
    .split(/[,\s]+/)
    .map(item => item.trim())
    .filter(Boolean);
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

function runsForJob(rows, jobId) {
  if (!jobId) return [];
  return (rows || []).filter(row => String(row?.job_id || "") === String(jobId));
}

function emptyThesisObservability(jobId, loadError = "") {
  return {
    meta: { source: "thesis_observability_readmodel", job_id: jobId, read_only: true, load_error: loadError },
    calls: [],
    usage_daily: [],
    totals: { overall: { calls: 0, total_quota_tokens: 0, cost_usd: 0 } },
  };
}

function adaptThesisReadModel(model) {
  const meta = model.meta || {};
  const document = model.document || {};
  const selectedExperimentId = String(meta.selected?.experiment_id || "");
  const memoryScope = String(meta.memory_scope || (selectedExperimentId ? "selected_run" : "project"));
  const memorySource = model.project_memory || (memoryScope === "selected_run" ? model.run_memory : model.runtime_memory) || {};
  const projectMemory = {
    glossary: memorySource.glossary_entries || [],
    entities: memorySource.entities || [],
    relations: memorySource.entity_relations || [],
    summaries: memorySource.summaries || [],
  };
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
        selected: meta.selected || {},
        memory_scope: memoryScope,
      },
      read_only: true,
    },
    chapters: model.chapters || [],
    blocks,
    glossary: projectMemory.glossary,
    entities: projectMemory.entities,
    relations: projectMemory.relations,
    summaries: projectMemory.summaries,
    references: [],
    evalOnly: model.eval_only || { gold_glossary: [], references: [] },
    translations: model.translations || {},
    runMemory: {
      scope: model.run_memory?.scope || {},
      glossary: model.run_memory?.glossary_entries || [],
      entities: model.run_memory?.entities || [],
      relations: model.run_memory?.entity_relations || [],
      summaries: model.run_memory?.summaries || [],
    },
    registryGlossary: model.runtime_memory?.glossary_entries || [],
    review: { blocks: {}, references: {}, summaries: {} },
    jobs: meta.available_runs || [],
    history: { can_undo: false, can_redo: false, undo_top: null, redo_top: null, recent: [] },
    errors: [],
  };
}

function worstOverlayStatus(statuses) {
  const order = ["localization_mismatch", "localization_source_warning", "drift", "low_coverage", "undetected", "localized_only", "localized", "consistent", "unscored"];
  const values = (statuses || []).filter(Boolean);
  return order.find(status => values.includes(status)) || values[0] || "unscored";
}

function overlayStatusByConfig(groupsByConfig, itemId, bucketName) {
  const result = {};
  Object.entries(groupsByConfig || {}).forEach(([config, cfg]) => {
    const bucket = cfg?.[bucketName] || {};
    const spans = bucket[itemId]?.occurrences || bucket[itemId]?.mentions || [];
    const cascadeSpans = spans.filter(item => String(item.mark_source || "").startsWith("cascade_"));
    const statusSource = cascadeSpans.length ? cascadeSpans : spans;
    const statuses = statusSource.map(item => item.status).filter(Boolean);
    if (statuses.length) result[config] = worstOverlayStatus(statuses);
  });
  return result;
}

function overlayTargetGroups(groupsByConfig, itemId, bucketName) {
  const result = {};
  Object.entries(groupsByConfig || {}).forEach(([config, cfg]) => {
    const bucket = cfg?.[bucketName] || {};
    const row = bucket[itemId];
    if (row) result[config] = row;
  });
  return result;
}

function normalizedGlossarySource(value) {
  return String(value || "")
    .normalize("NFKC")
    .trim()
    .replace(/\s+/gu, " ")
    .toLocaleLowerCase("en-US");
}

function localizationTermDetail(termId, sourceTerm, item) {
  const acceptedForms = item?.accepted_forms || [];
  return {
    term_id: termId,
    source_term: item?.source_term || sourceTerm || termId,
    expected_target: item?.accepted_form || acceptedForms[0] || "",
    allowed_variants: acceptedForms,
    provenance: { branch: "localization", label: "Localization" },
  };
}

function glossarySourceFromOverlayRow(row) {
  const canonicalSource = String(row?.source_term || "").trim();
  if (canonicalSource) return canonicalSource;
  const candidates = (row?.occurrences || [])
    .map(item => item?.source_term)
    .map(value => String(value || "").trim())
    .filter(Boolean);
  const byKey = new Map();
  candidates.forEach(value => byKey.set(normalizedGlossarySource(value), value));
  return byKey.size === 1 ? [...byKey.values()][0] : "";
}

function buildGlossaryOverlayBridge(glossary, registryGlossary, overlay) {
  const activeIds = new Set();
  const activeIdsBySource = new Map();
  (glossary || []).forEach(term => {
    const termId = String(term?.term_id || term?.glossary_id || "");
    const sourceKey = normalizedGlossarySource(term?.source_term);
    if (!termId) return;
    activeIds.add(termId);
    if (!sourceKey) return;
    const ids = activeIdsBySource.get(sourceKey) || [];
    ids.push(termId);
    activeIdsBySource.set(sourceKey, ids);
  });

  const registryById = new Map();
  const registryBySource = new Map();
  (registryGlossary || []).forEach(term => {
    const termId = String(term?.term_id || term?.glossary_id || "");
    const sourceKey = normalizedGlossarySource(term?.source_term);
    if (termId) registryById.set(termId, term);
    if (!sourceKey) return;
    const rows = registryBySource.get(sourceKey) || [];
    rows.push(term);
    registryBySource.set(sourceKey, rows);
  });

  const activeIdByOverlayId = {};
  const sourceTermByOverlayId = {};
  const registryTermByOverlayId = {};
  const sourceOccurrencesByOverlayId = {};
  Object.entries(overlay?.source?.glossary_by_id || {}).forEach(([overlayId, row]) => {
    sourceOccurrencesByOverlayId[overlayId] = row?.occurrences || [];
    const sourceTerm = glossarySourceFromOverlayRow(row);
    const sourceKey = normalizedGlossarySource(sourceTerm);
    if (sourceTerm) sourceTermByOverlayId[overlayId] = sourceTerm;
    if (activeIds.has(overlayId)) {
      activeIdByOverlayId[overlayId] = overlayId;
      return;
    }
    const exactRegistryTerm = registryById.get(overlayId);
    if (!sourceKey) {
      if (exactRegistryTerm) registryTermByOverlayId[overlayId] = exactRegistryTerm;
      return;
    }
    const matchingActiveIds = [...new Set(activeIdsBySource.get(sourceKey) || [])];
    if (matchingActiveIds.length === 1) {
      activeIdByOverlayId[overlayId] = matchingActiveIds[0];
      return;
    }
    const sourceMatches = registryBySource.get(sourceKey) || [];
    const registryTerm = exactRegistryTerm || (sourceMatches.length === 1 ? sourceMatches[0] : null);
    if (registryTerm) registryTermByOverlayId[overlayId] = registryTerm;
  });
  const overlayIdsByActiveId = {};
  Object.entries(activeIdByOverlayId).forEach(([overlayId, activeId]) => {
    if (!overlayIdsByActiveId[activeId]) overlayIdsByActiveId[activeId] = [];
    overlayIdsByActiveId[activeId].push(overlayId);
  });
  Object.values(overlayIdsByActiveId).forEach(ids => ids.sort());
  return {
    activeIdByOverlayId,
    overlayIdsByActiveId,
    sourceTermByOverlayId,
    registryTermByOverlayId,
    sourceOccurrencesByOverlayId,
  };
}

function overlayRowsForActiveId(bucket, activeId, glossaryBridge) {
  const overlayIds = glossaryBridge?.overlayIdsByActiveId?.[activeId] || [];
  const ids = overlayIds.length ? overlayIds : [activeId];
  return ids.map(id => bucket?.[id]).filter(Boolean);
}

function overlayStatusByConfigForActiveId(groupsByConfig, activeId, bucketName, glossaryBridge) {
  const result = {};
  Object.entries(groupsByConfig || {}).forEach(([config, cfg]) => {
    const rows = overlayRowsForActiveId(cfg?.[bucketName] || {}, activeId, glossaryBridge);
    const spans = rows.flatMap(row => row?.occurrences || row?.mentions || []);
    const cascadeSpans = spans.filter(item => String(item.mark_source || "").startsWith("cascade_"));
    const statuses = (cascadeSpans.length ? cascadeSpans : spans).map(item => item.status).filter(Boolean);
    if (statuses.length) result[config] = worstOverlayStatus(statuses);
  });
  return result;
}

function overlayTargetGroupsForActiveId(groupsByConfig, activeId, bucketName, glossaryBridge) {
  const result = {};
  Object.entries(groupsByConfig || {}).forEach(([config, cfg]) => {
    const rows = overlayRowsForActiveId(cfg?.[bucketName] || {}, activeId, glossaryBridge);
    if (!rows.length) return;
    result[config] = {
      occurrences: rows.flatMap(row => row?.occurrences || []),
      mentions: rows.flatMap(row => row?.mentions || []),
    };
  });
  return result;
}

function targetSpansForBlock(blockId, translations, overlay, glossaryBridge) {
  const spansByConfig = {};
  Object.entries(translations || {}).forEach(([config]) => {
    const cfg = overlay?.target_by_config?.[config] || {};
    const spans = [];
    Object.entries(cfg.glossary_by_id || {}).forEach(([termId, row]) => {
      const linkedActiveTermId = glossaryBridge?.activeIdByOverlayId?.[termId];
      const activeTermId = linkedActiveTermId || termId;
      const sourceTerm = glossaryBridge?.sourceTermByOverlayId?.[termId] || "";
      const registryTerm = glossaryBridge?.registryTermByOverlayId?.[termId];
      const sourceOccurrences = glossaryBridge?.sourceOccurrencesByOverlayId?.[termId] || [];
      (row.occurrences || []).forEach(item => {
        if (item.block_id !== blockId) return;
        spans.push({
          start: item.span?.[0] || 0,
          end: item.span?.[1] || 0,
          kind: "glossary",
          id: activeTermId,
          registry_id: activeTermId === termId ? undefined : termId,
          source_term: item.source_term || sourceTerm || undefined,
          registry_only: !linkedActiveTermId && !!registryTerm,
          term_detail: registryTerm || localizationTermDetail(activeTermId, sourceTerm, item),
          detail_occurrences: sourceOccurrences,
          status: item.status || "unscored",
          display_status: item.display_status,
          localization_status: item.localization_status,
          reference_status: item.reference_status,
          adherence_label: item.adherence_label,
          accepted_forms: item.accepted_forms || [],
          accepted_form: item.accepted_form,
          constraint_strength: item.constraint_strength,
          label: `${item.surface || item.matched_form || termId}`,
          surface: item.surface,
          matched_form: item.matched_form,
          forms_used: item.forms_used || {},
          forms_source: item.forms_source,
          scored: !!item.scored,
          provenance: item.provenance,
          mark_source: item.mark_source,
          located_by: item.located_by,
          occ_id: item.occ_id,
          masquerade_suspect: !!item.masquerade_suspect,
          clean_text_fallback: !!item.clean_text_fallback,
          gpt_fallback: !!item.gpt_fallback,
          cross_term_overlap: !!item.cross_term_overlap,
          target: true,
        });
      });
    });
    Object.entries(cfg.entities_by_id || {}).forEach(([entityId, row]) => {
      (row.mentions || []).forEach(item => {
        if (item.block_id !== blockId) return;
        spans.push({
          start: item.span?.[0] || 0,
          end: item.span?.[1] || 0,
          kind: "entity",
          id: entityId,
          status: item.status || "unscored",
          display_status: item.display_status,
          localization_status: item.localization_status,
          label: `${item.surface || item.matched_form || entityId}`,
          surface: item.surface,
          matched_form: item.matched_form,
          forms_used: item.forms_used || {},
          forms_source: item.forms_source,
          scored: !!item.scored,
          provenance: item.provenance,
          mark_source: item.mark_source,
          located_by: item.located_by,
          occ_id: item.occ_id,
          masquerade_suspect: !!item.masquerade_suspect,
          clean_text_fallback: !!item.clean_text_fallback,
          gpt_fallback: !!item.gpt_fallback,
          cross_term_overlap: !!item.cross_term_overlap,
          target: true,
        });
      });
    });
    spansByConfig[config] = spans;
  });
  return spansByConfig;
}

function sourceSpansForBlock(blockId, sourceGlossary, glossaryBridge) {
  const spans = [];
  Object.entries(sourceGlossary || {}).forEach(([termId, row]) => {
    const linkedActiveTermId = glossaryBridge?.activeIdByOverlayId?.[termId];
    const activeTermId = linkedActiveTermId || termId;
    const sourceTerm = glossaryBridge?.sourceTermByOverlayId?.[termId] || "";
    const registryTerm = glossaryBridge?.registryTermByOverlayId?.[termId];
    const sourceOccurrences = row?.occurrences || [];
    sourceOccurrences.forEach(item => {
      if (item.block_id !== blockId) return;
      spans.push({
        start: item.span?.[0] || 0,
        end: item.span?.[1] || 0,
        kind: "glossary",
        id: activeTermId,
        registry_id: activeTermId === termId ? undefined : termId,
        source_term: item.source_term || sourceTerm || undefined,
        registry_only: !linkedActiveTermId && !!registryTerm,
        term_detail: registryTerm || localizationTermDetail(activeTermId, sourceTerm, item),
        detail_occurrences: sourceOccurrences,
        status: item.status || "localized",
        display_status: item.display_status,
        localization_status: item.localization_status,
        reference_status: item.reference_status,
        accepted_forms: item.accepted_forms || [],
        accepted_form: item.accepted_form,
        mismatch_configs: item.mismatch_configs || [],
        label: `${item.surface || item.source_term || sourceTerm || termId}`,
        surface: item.surface,
        provenance: item.provenance,
        mark_source: item.mark_source,
        located_by: item.located_by,
        occ_id: item.occ_id,
        configs: item.configs || [],
        target: false,
      });
    });
  });
  return spans;
}

function applyRegistryOverlay(adapted, overlay) {
  if (!overlay) return adapted;
  const localizationOnly = overlay.meta?.overlay_mode === "localization";
  const sourceGlossary = overlay.source?.glossary_by_id || {};
  const sourceEntities = overlay.source?.entities_by_id || {};
  const targetByConfig = overlay.target_by_config || {};
  const glossaryBridge = buildGlossaryOverlayBridge(
    adapted.glossary,
    adapted.registryGlossary,
    overlay,
  );
  const glossary = (adapted.glossary || []).map(term => {
    const id = term.term_id || term.glossary_id;
    const sourceRows = overlayRowsForActiveId(sourceGlossary, id, glossaryBridge);
    const sourceOccurrences = sourceRows.flatMap(row => row?.occurrences || []);
    const statusByConfig = localizationOnly
      ? overlayStatusByConfigForActiveId(targetByConfig, id, "glossary_by_id", glossaryBridge)
      : overlayStatusByConfig(targetByConfig, id, "glossary_by_id");
    return {
      ...term,
      occurrences: localizationOnly ? sourceOccurrences : (sourceOccurrences.length ? sourceOccurrences : term.occurrences || []),
      target_occurrences_by_config: localizationOnly
        ? overlayTargetGroupsForActiveId(targetByConfig, id, "glossary_by_id", glossaryBridge)
        : overlayTargetGroups(targetByConfig, id, "glossary_by_id"),
      overlay_status_by_config: statusByConfig,
      overlay_status: localizationOnly
        ? (Object.keys(statusByConfig).length ? worstOverlayStatus(Object.values(statusByConfig)) : "")
        : worstOverlayStatus(Object.values(statusByConfig)),
      overlay_provenance: overlay.meta,
    };
  });
  const entities = (adapted.entities || []).map(entity => {
    const id = entity.entity_id;
    const source = sourceEntities[id] || {};
    const statusByConfig = overlayStatusByConfig(targetByConfig, id, "entities_by_id");
    return {
      ...entity,
      mentions: localizationOnly ? (source.mentions || []) : (source.mentions || entity.mentions || []),
      target_mentions_by_config: overlayTargetGroups(targetByConfig, id, "entities_by_id"),
      overlay_status_by_config: statusByConfig,
      overlay_status: localizationOnly
        ? (Object.keys(statusByConfig).length ? worstOverlayStatus(Object.values(statusByConfig)) : "")
        : worstOverlayStatus(Object.values(statusByConfig)),
      overlay_provenance: overlay.meta,
    };
  });
  const blocks = (adapted.blocks || []).map(block => {
    const targetSpans = targetSpansForBlock(
      block.block_id,
      block.translations,
      overlay,
      glossaryBridge,
    );
    const translations = {};
    Object.entries(block.translations || {}).forEach(([config, row]) => {
      translations[config] = {
        ...row,
        target_spans: targetSpans[config] || [],
      };
    });
    const allTargetSpans = Object.values(targetSpans).flat();
    const localizationSourceSpans = sourceSpansForBlock(block.block_id, sourceGlossary, glossaryBridge);
    const sourceSpans = localizationOnly
      ? localizationSourceSpans
      : [
          ...Object.values(sourceGlossary).flatMap(row => (row.occurrences || []).filter(item => item.block_id === block.block_id)),
          ...Object.values(sourceEntities).flatMap(row => (row.mentions || []).filter(item => item.block_id === block.block_id)),
        ];
    const sourceStatuses = sourceSpans.map(item => item.status);
    const targetStatuses = allTargetSpans.map(item => item.status);
    const statuses = [...sourceStatuses, ...targetStatuses].filter(Boolean);
    return {
      ...block,
      translations,
      overlay_mode: localizationOnly ? "localization" : undefined,
      source_overlay_spans: localizationOnly ? localizationSourceSpans : undefined,
      overlay_status: localizationOnly
        ? (statuses.length ? worstOverlayStatus(statuses) : "")
        : worstOverlayStatus(statuses),
      overlay_counts: {
        source: sourceSpans.length,
        target: allTargetSpans.length,
        mismatch: statuses.filter(status => status === "localization_mismatch").length,
        drift: localizationOnly
          ? 0
          : statuses.filter(status => status === "drift" || status === "low_coverage").length,
      },
    };
  });
  return { ...adapted, glossary, entities, blocks, registryOverlay: overlay };
}

/* build annotation spans for a block from glossary + entities, with stale detection */
function buildSpans(block, glossary, entities) {
  if (!block) return [];
  const spans = [];
  const sourceOverlaySpans = block.overlay_mode === "localization" && Array.isArray(block.source_overlay_spans)
    ? block.source_overlay_spans
    : null;
  if (sourceOverlaySpans) {
    sourceOverlaySpans.forEach(item => {
      if (!Number.isInteger(item.start) || !Number.isInteger(item.end) || item.end <= item.start) return;
      const cur = block.clean_text.slice(item.start, item.end);
      spans.push({
        ...item,
        stale: cur.toLowerCase() !== String(item.surface || item.source_term || "").toLowerCase(),
      });
    });
  } else glossary.forEach(t => (t.occurrences || []).forEach(o => {
    if (o.block_id !== block.block_id) return;
    if (!Array.isArray(o.span) || o.span.length < 2) return;
    const cur = block.clean_text.slice(o.span[0], o.span[1]);
    const status = o.status || t.overlay_status || "unscored";
    spans.push({
      start: o.span[0],
      end: o.span[1],
      kind: "glossary",
      label: `${t.source_term} -> ${t.expected_target || "target needed"}`,
      id: t.term_id,
      status,
      display_status: o.display_status,
      localization_status: o.localization_status,
      status_by_config: t.overlay_status_by_config || {},
      provenance: o.provenance || t.provenance?.label || "agent-built",
      mark_source: o.mark_source,
      located_by: o.located_by,
      surface: o.surface,
      source_term: o.source_term || t.source_term,
      occ_id: o.occ_id,
      configs: o.configs || [],
      reference_status: o.reference_status,
      accepted_forms: o.accepted_forms || [],
      accepted_form: o.accepted_form,
      mismatch_configs: o.mismatch_configs || [],
      stale: cur.toLowerCase() !== String(o.surface || t.source_term || "").toLowerCase(),
    });
  }));
  entities.forEach(e => (e.mentions || []).forEach(m => {
    if (m.block_id !== block.block_id) return;
    if (!Array.isArray(m.span) || m.span.length < 2) return;
    const cur = block.clean_text.slice(m.span[0], m.span[1]);
    const status = m.status || e.overlay_status || "unscored";
    spans.push({
      start: m.span[0],
      end: m.span[1],
      kind: "entity",
      label: `${e.canonical_source} -> ${e.canonical_target || "target needed"}`,
      id: e.entity_id,
      status,
      status_by_config: e.overlay_status_by_config || {},
      provenance: e.provenance?.label || "agent-built",
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

function Modal({ title, icon: I, tone, children, onClose, actions, className = "" }) {
  const dialogRef = React.useRef(null);
  const closeRef = React.useRef(onClose);
  const titleId = React.useId();

  closeRef.current = onClose;

  React.useEffect(() => {
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement;
    if (!dialog) return undefined;
    const focusableSelector = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';
    const focusable = () => Array.from(dialog.querySelectorAll(focusableSelector)).filter(element => element.getClientRects().length > 0);
    (focusable()[0] || dialog).focus();

    function onKeyDown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current?.();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);

  return (
    <div className="modal-scrim" onMouseDown={onClose}>
      <div ref={dialogRef} className={`modal${className ? ` ${className}` : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} onMouseDown={e => e.stopPropagation()}>
        <div className="modal-head">
          <span className={"modal-ic " + (tone || "")}>{I && <I size={16} />}</span>
          <span className="modal-title" id={titleId}>{title}</span>
          <button className="modal-x" type="button" aria-label={uiText("Đóng", "Close")} onClick={onClose}><Ic.x size={13} /></button>
        </div>
        <div className="modal-body">{children}</div>
        <div className="modal-foot">{actions}</div>
      </div>
    </div>
  );
}

function historyTip(prefix, event) {
  return event?.label ? `${prefix}: ${event.label}` : `${prefix} ${uiText("không khả dụng", "unavailable")}`;
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

const WORKSPACE_VIEW_MODES = [
  { id: "block", vi: "Block", en: "Block" },
  { id: "chapter", vi: "Chương", en: "Chapter" },
  { id: "book", vi: "Sách", en: "Book" },
  { id: "structure", vi: "Cấu trúc", en: "Structure" },
  { id: "memory", vi: "Bộ nhớ", en: "Memory" },
  { id: "preview", vi: "Bản dịch", en: "Preview" },
];
const WORKSPACE_RUN_MODES = [
  { id: "console", vi: "Console", en: "Console" },
  { id: "report", vi: "Báo cáo", en: "Report" },
];

function TopProjectPicker({ docId, projects, onSelectProject, onOpenProjectSource }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="tb-project-wrap">
      <button className="tb-project-pick" type="button" onClick={() => setOpen(value => !value)} aria-expanded={open}>
        <Ic.folder size={13} className="faint" />
        <span className="mono">{docId || uiText("chưa có tài liệu", "no document")}</span>
        <Ic.chevDown size={11} className="faint" />
      </button>
      {open && (<>
        <div className="menu-scrim" onClick={() => setOpen(false)} />
        <div className="tb-project-menu">
          <div className="proj-menu-sec">{uiText("Project gần đây", "Recent projects")}</div>
          {(projects || []).map(project => (
            <button key={project.doc_id} className={"proj-menu-item" + (project.doc_id === docId ? " cur" : "")}
              onClick={() => { setOpen(false); onSelectProject(project.doc_id); }}>
              <Ic.doc size={13} className="faint" />
              <span className="pm-id mono">{project.doc_id}</span>
              <span className="pm-meta">{project.status}</span>
              {project.doc_id === docId && <Ic.check size={13} className="pm-cur-ic" />}
            </button>
          ))}
          <div className="divider" />
          <button className="proj-menu-item" onClick={() => { setOpen(false); onOpenProjectSource(); }}>
            <Ic.folder size={13} className="faint" /><span>{uiText("Project / Nguồn", "Project / Source")}</span>
          </button>
        </div>
      </>)}
    </div>
  );
}

function WorkspaceModeNav({ mode, onModeChange, showStructure = true }) {
  function renderMode(item) {
    return (
      <button key={item.id} className={"top-mode-btn" + (mode === item.id ? " on" : "")}
        type="button" role="tab" aria-selected={mode === item.id} onClick={() => onModeChange(item.id)}>
        {uiText(item.vi, item.en)}
      </button>
    );
  }
  return (
    <nav className="top-mode-nav" aria-label={uiText("Các chế độ workspace", "Workspace views")}>
      <div className="top-mode-group" role="tablist" aria-label={uiText("Chế độ tài liệu", "Document views")}>
        {WORKSPACE_VIEW_MODES.filter(item => item.id !== "structure" || showStructure).map(renderMode)}
      </div>
      <span className="top-mode-sep" />
      <div className="top-mode-group run" role="tablist" aria-label={uiText("Chế độ run", "Run views")}>
        {WORKSPACE_RUN_MODES.map(renderMode)}
      </div>
    </nav>
  );
}

function TopBar({
  docId, projects, mode, onModeChange, onSelectProject, onOpenProjectSource, onQuickImport,
  leftPanelOpen, rightPanelOpen, onToggleLeftPanel, onToggleRightPanel,
  dirty, lastSaved, onValidate, onExportOption, onFreeze, onUndo, onRedo, history,
  freezeReady, freezeReasons, previewReadOnly, canExportPreview, appVersion,
  locale, onLocaleChange,
}) {
  const [exportOpen, setExportOpen] = useState(false);
  const canUndo = !!history?.can_undo && !dirty && !previewReadOnly;
  const canRedo = !!history?.can_redo && !dirty && !previewReadOnly;
  const readOnlyTip = uiText("Không thể chỉnh sửa trong chế độ chỉ đọc.", "Editing is disabled in viewer mode.");
  const packageDisabled = false;
  const qcDisabled = false;
  const apiVersion = appVersion?.backend_version || appVersion?.version || "unknown";
  const versionMismatch = apiVersion !== "unknown" && apiVersion !== UI_VERSION;
  const previewDisabled = !canExportPreview;
  const savedSeconds = Math.max(0, Number(lastSaved) || 0);
  const savedLabel = savedSeconds < 3
    ? uiText("vừa xong", "just now")
    : savedSeconds < 60
      ? uiText("{count} giây trước", "{count}s ago", { count: savedSeconds })
      : uiText("{count} phút trước", "{count}m ago", { count: Math.floor(savedSeconds / 60) });
  function chooseExport(kind) {
    setExportOpen(false);
    onExportOption(kind);
  }
  const gitSha = appVersion?.git_sha && appVersion.git_sha !== "unknown" ? appVersion.git_sha : "";
  const userName = currentUser();
  const userInitial = (userName[0] || "M").toUpperCase();
  function runAction(fn) { setExportOpen(false); if (fn) fn(); }
  return (
    <div className="topbar">
      <div className="tb-left">
        <span className="tb-logo" aria-label="Thesis Runtime Tool">▧</span>
        <TopProjectPicker docId={docId} projects={projects} onSelectProject={onSelectProject} onOpenProjectSource={onOpenProjectSource} />
        <button className="btn sm primary tb-import" type="button" onClick={onQuickImport}>
          <Ic.upload size={12} />{uiText("Nhập tài liệu", "Import document")}
        </button>
      </div>

      <WorkspaceModeNav mode={mode} onModeChange={onModeChange} showStructure={!isThesisDatasetId(docId)} />

      <div className="tb-right">
        {!(["console", "structure"].includes(mode)) && <div className="panel-toggle-group">
          <button className={"btn icon-only tip" + (leftPanelOpen ? " is-on" : "")} type="button"
            data-tip={leftPanelOpen ? uiText("Ẩn điều hướng chương", "Hide chapter navigation") : uiText("Hiện điều hướng chương", "Show chapter navigation")}
            aria-label={uiText("Bật/tắt điều hướng chương", "Toggle chapter navigation")} aria-pressed={leftPanelOpen} onClick={onToggleLeftPanel}>
            <Ic.list size={13} />
          </button>
          {mode !== "memory" && (
            <button className={"btn icon-only tip" + (rightPanelOpen ? " is-on" : "")} type="button"
              data-tip={rightPanelOpen ? uiText("Ẩn bảng ngữ cảnh", "Hide context inspector") : uiText("Hiện bảng ngữ cảnh", "Show context inspector")}
              aria-label={uiText("Bật/tắt bảng ngữ cảnh", "Toggle context inspector")} aria-pressed={rightPanelOpen} onClick={onToggleRightPanel}>
              <Ic.layers size={13} />
            </button>
          )}
        </div>}
        <span className="autosave">
          {dirty ? <><span className="as-spin" />{uiText("đang lưu...", "saving...")}</> : <><Ic.check size={12} className="as-ok" />{uiText("đã lưu", "saved")} {savedLabel}</>}
        </span>
        <div className="undo-group">
          <button className="btn icon-only tip" disabled={!canUndo} data-tip={previewReadOnly ? readOnlyTip : dirty ? uiText("Chờ lượt lưu hiện tại hoàn tất", "Wait for current save to finish") : historyTip(uiText("Hoàn tác", "Undo"), history?.undo_top)} onClick={onUndo} aria-label={uiText("Hoàn tác", "Undo")}>
            <Ic.undo size={13} />
          </button>
          <button className="btn icon-only tip" disabled={!canRedo} data-tip={previewReadOnly ? readOnlyTip : dirty ? uiText("Chờ lượt lưu hiện tại hoàn tất", "Wait for current save to finish") : historyTip(uiText("Làm lại", "Redo"), history?.redo_top)} onClick={onRedo} aria-label={uiText("Làm lại", "Redo")}>
            <Ic.redo size={13} />
          </button>
        </div>
        <ThesisLocaleSwitch compact locale={locale} onChange={onLocaleChange} />
        <div className="export-menu-wrap">
          <button className="btn icon-only tb-menu-btn" onClick={() => setExportOpen(v => !v)} aria-label={uiText("Menu và tài khoản", "Menu & account")}>
            <span className="ua">{userInitial}</span><Ic.chevDown size={11} className="faint" />
          </button>
          {exportOpen && (<>
            <div className="menu-scrim" onClick={() => setExportOpen(false)} />
            <div className="export-menu tb-menu">
              <div className="tb-menu-user">
                <span className="ua">{userInitial}</span>
                <div><b>{userName}</b><em>{uiText("người dùng cục bộ", "local user")}</em></div>
              </div>
              <div className="tb-menu-meta">
                <span>UI {UI_VERSION} · API {apiVersion}</span>
                {gitSha ? <span>git {gitSha}</span> : null}
                <span>{previewReadOnly ? uiText("chế độ chỉ đọc", "viewer mode") : uiText("bản làm việc · tự động lưu", "working copy · autosaved")}</span>
              </div>
              <div className="tb-menu-div" />
              <button disabled={previewReadOnly} onClick={() => runAction(onValidate)}>
                <Ic.checkCircle size={13} /><span><b>{uiText("Kiểm tra", "Validate")}</b><em>{uiText("Nhóm lỗi và gate", "Grouped issues & gates")}</em></span>
              </button>
              <div className="tb-menu-label">{uiText("Xuất", "Export")}</div>
              <button disabled={!docId} onClick={() => chooseExport("package")}>
                <Ic.layers size={13} /><span><b>{uiText("Gói dataset", "Dataset package")}</b><em>{uiText("Nguồn + dataset + trạng thái làm việc + QC", "Source + dataset + working state + QC")}</em></span>
              </button>
              <button disabled={!docId} onClick={() => chooseExport("qc")}>
                <Ic.checkCircle size={13} /><span><b>{uiText("Báo cáo QC", "QC report")}</b><em>{uiText("Số lượng, kiểm tra, review và gate đóng băng", "Counts, validation, review, freeze gates")}</em></span>
              </button>
              <button disabled={!docId} onClick={() => chooseExport("dataset-previews")}>
                <Ic.book size={13} /><span><b>{uiText("Dataset + mọi bản dịch", "Dataset + all previews")}</b><em>{uiText("Gói dữ liệu + toàn bộ bản dịch đã lưu", "Package + all translated previews")}</em></span>
              </button>
              <button disabled={!docId || previewDisabled} onClick={() => chooseExport("preview")}>
                <Ic.eye size={13} /><span><b>{uiText("Bản dịch xem trước", "Translation preview")}</b><em>{uiText("Run xem trước hiện tại", "Current preview run")}</em></span>
              </button>
              <div className="tb-menu-div" />
              <button disabled={previewReadOnly || !freezeReady} onClick={() => runAction(onFreeze)}>
                <Ic.snow size={13} /><span><b>{uiText("Đóng băng snapshot", "Freeze snapshot")}</b><em>{freezeReady ? uiText("Snapshot có phiên bản sau khi qua gate", "Versioned snapshot after gates pass") : uiText("Bị chặn: ", "Blocked: ") + freezeReasons.join(" · ")}</em></span>
              </button>
            </div>
          </>)}
        </div>
      </div>
    </div>
  );
}

function PreviewRightPanel({ docInfo, block, preview }) {
  const run = preview?.run || null;
  const configs = run?.configs || [];
  const mode = configs.length > 1 ? uiText("đối chiếu", "comparison") : configs.length === 1 ? uiText("một bản dịch", "single translation") : uiText("chưa có kết quả", "no result");
  return (
    <div className="col col-right preview-info-panel">
      <div className="preview-info-head">
        <Ic.eye size={15} />
        <div>
          <b>{uiText("Kết quả dịch", "Translation results")}</b>
        </div>
      </div>
      <div className="preview-info-card">
        <p>{configs.length > 1
          ? uiText("Các phiên bản dịch đã lưu được hiển thị song song.", "Stored translation versions are shown side by side.")
          : configs.length === 1
            ? uiText("Bản dịch đã lưu được hiển thị cạnh nguồn.", "The stored translation is shown beside its source.")
            : uiText("Chương này chưa có bản dịch đã lưu.", "No stored translation is available for this chapter.")}</p>
      </div>
      <div className="preview-info-list">
        <div><span>{uiText("dự án", "project")}</span><b className="mono">{docInfo?.doc_id || ""}</b></div>
        <div><span>{uiText("chương", "chapter")}</span><b className="mono">{run?.chapter_id || block?.chapter_id || ""}</b></div>
        <div><span>{uiText("block đang chọn", "active block")}</span><b className="mono">{block?.block_id || ""}</b></div>
        <div><span>{uiText("chế độ", "mode")}</span><b>{mode}</b></div>
        <div><span>{uiText("phiên bản", "versions")}</span><b className="mono">{configs.join(" / ") || uiText("không có", "none")}</b></div>
        <div><span>{uiText("độ phủ", "coverage")}</span><b className="mono">{run ? `${run.translated_block_count || 0}/${run.block_count || 0} block` : "0/0 block"}</b></div>
      </div>
    </div>
  );
}

function StartupState({ title, message, action, onAction, secondary, secondaryAction, onSecondaryAction, locale, onLocaleChange }) {
  return (
    <div className="project-screen">
      <div className="project-wrap" style={{ maxWidth: 760 }}>
        <div className="project-headline">
          <div>
            <div className="project-kicker">Thesis Runtime Tool</div>
            <h1>{title}</h1>
            <p>{message}</p>
            {secondary && <p className="muted">{secondary}</p>}
          </div>
          <div className="startup-actions">
            <ThesisLocaleSwitch locale={locale} onChange={onLocaleChange} />
            {secondaryAction && <button className="btn" onClick={onSecondaryAction}>{secondaryAction}</button>}
            {action && <button className="btn primary" onClick={onAction}>{action}</button>}
          </div>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [uiLocale, setUiLocale] = useThesisLocale();
  const [view, setView] = useState(viewFromLocation);
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
  const [thesisBaseDataset, setThesisBaseDataset] = useState(null);
  const [projectRuntime, setProjectRuntime] = useState(null);
  const [appVersion, setAppVersion] = useState({ ui_version: UI_VERSION, backend_version: "unknown", git_sha: "unknown" });
  const [thesisRuns, setThesisRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState(null);
  const [selectedRunLog, setSelectedRunLog] = useState({ run_id: null, log: "", offset: 0, running: false, status: "" });
  const [selectedRunEvents, setSelectedRunEvents] = useState({ run_id: null, events: [], offset: 0, running: false, status: "", aggregate: emptyRunEventAggregate() });
  const [runBlockPreview, setRunBlockPreview] = useState([]);
  const [runWatchlist, setRunWatchlist] = useState([]);
  const [runReportSummary, setRunReportSummary] = useState(null);
  const [dichForm, setDichForm] = useState({ chapters: "d2l_preface", profile: "technical_d2l_v1", db: "data/jobs/d2l_p1/memory.sqlite3", budget_cap_usd: 1.5, with_s0: false });
  const [runPromptPreview, setRunPromptPreview] = useState(null);
  const [runBusy, setRunBusy] = useState(false);
  const [runError, setRunError] = useState("");
  const [runForm, setRunForm] = useState({
    script: "run_translate",
    chapters: "ch02 ch03",
    configs: "S0 S1",
    profile: "literary_v1",
    experiment: "translate_run",
    cache: "data/jobs/translate_cache.sqlite3",
    allow_api: false,
  });
  const [selectedCallId, setSelectedCallId] = useState(null);
  const [selectedCallDetail, setSelectedCallDetail] = useState(null);
  const [callDetailLoading, setCallDetailLoading] = useState(false);
  const [review, setReview] = useState({ blocks: {}, references: {}, summaries: {} });
  const [historyState, setHistoryState] = useState({ can_undo: false, can_redo: false, undo_top: null, redo_top: null, recent: [] });
  const [errors, setErrors] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [filters, setFilters] = useState(new Set());
  const [rightOpenTabs, setRightOpenTabs] = useState(["glossary"]);
  const [memoryFocusKind, setMemoryFocusKind] = useState("glossary");
  const [registryOverlayLoading, setRegistryOverlayLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [centerMode, setCenterModeState] = useState(() => {
    const saved = localStorage.getItem(STORAGE_CENTER_MODE);
    return ["block", "chapter", "book", "structure", "memory", "preview"].includes(saved) ? saved : "chapter";
  });
  const [leftPanelOpen, setLeftPanelOpen] = useState(() => localStorage.getItem(STORAGE_LEFT_PANEL) !== "false");
  const [rightPanelOpen, setRightPanelOpen] = useState(() => localStorage.getItem(STORAGE_RIGHT_PANEL) !== "false");
  const [rightPanelExpanded, setRightPanelExpanded] = useState(() => localStorage.getItem(STORAGE_RIGHT_PANEL_EXPANDED) === "true");
  const [toasts, setToasts] = useState([]);
  const [modal, setModal] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [schemaMigrating, setSchemaMigrating] = useState(false);
  const [currentPreviewRun, setCurrentPreviewRun] = useState(null);
  const [lastSaved, setLastSaved] = useState(0);
  const [loading, setLoading] = useState(true);
  const [bootError, setBootError] = useState(null);
  const [activeDocId, setActiveDocId] = useState(localStorage.getItem(STORAGE_DOC) || "");
  const [focusedTermId, setFocusedTermId] = useState(null);
  const [focusedTermIndex, setFocusedTermIndex] = useState(0);
  const [focusedTermCount, setFocusedTermCount] = useState(0);
  const [focusedTermSurface, setFocusedTermSurface] = useState("");
  const savedAt = useRef(Date.now());
  const saveTimers = useRef({});
  const runLogOffsetRef = useRef(0);
  const runEventOffsetRef = useRef(0);
  const fullRegistryOverlayCacheRef = useRef(new Map());

  const navigateView = useCallback((nextView, { replace = false } = {}) => {
    const targetView = nextView === "project"
      ? "project"
      : nextView === "console"
        ? "console"
        : nextView === "report"
          ? "report"
          : "workspace";
    const baseUrl = `${window.location.pathname}${window.location.search}`;
    const targetUrl = targetView === "project"
      ? `${baseUrl}${PROJECT_ROUTE_HASH}`
      : targetView === "console"
        ? `${baseUrl}${CONSOLE_ROUTE_HASH}`
        : targetView === "report"
          ? `${baseUrl}${REPORT_ROUTE_HASH}`
        : baseUrl;
    const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (currentUrl !== targetUrl) {
      window.history[replace ? "replaceState" : "pushState"]({ view: targetView }, "", targetUrl);
    }
    setView(targetView);
  }, []);

  useEffect(() => {
    const syncViewFromLocation = () => setView(viewFromLocation());
    window.addEventListener("popstate", syncViewFromLocation);
    window.addEventListener("hashchange", syncViewFromLocation);
    return () => {
      window.removeEventListener("popstate", syncViewFromLocation);
      window.removeEventListener("hashchange", syncViewFromLocation);
    };
  }, []);

  useEffect(() => {
    if (view === "project" && isThesisDatasetId(activeDocId)) {
      navigateView("workspace", { replace: true });
    }
  }, [activeDocId, navigateView, view]);

  const refreshProjects = useCallback(async () => {
    const [legacyResult, thesisResult] = await Promise.allSettled([
      API.listProjects(),
      API.listThesisDatasets(),
    ]);
    if (legacyResult.status === "rejected" && thesisResult.status === "rejected") {
      throw new Error(`Could not load projects from ${API.baseUrl}.`);
    }
    const legacy = legacyResult.status === "fulfilled" ? legacyResult.value : [];
    const thesis = thesisResult.status === "fulfilled" ? thesisResult.value : [];
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
    const [dataset, runtime] = await Promise.all([
      API.getDataset(docId),
      API.getProjectRuntime(docId).catch(err => ({
        project_id: docId,
        prepared: false,
        load_error: errorMessage(err),
      })),
    ]);
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
    setProjectRuntime(runtime);
    let runtimeObservability = null;
    let runtimeRuns = [];
    if (runtime?.prepared && runtime?.job_id) {
      [runtimeObservability, runtimeRuns] = await Promise.all([
        API.getThesisObservability(runtime.job_id).catch(err => emptyThesisObservability(runtime.job_id, errorMessage(err))),
        API.listThesisRuns().then(rows => runsForJob(rows, runtime.job_id)).catch(() => []),
      ]);
    }
    setThesisObservability(runtimeObservability);
    setThesisBaseDataset(null);
    setThesisRuns(runtimeRuns);
    setSelectedRunId(null);
    setSelectedRunLog({ run_id: null, log: "", offset: 0, running: false, status: "" });
    setSelectedRunEvents({ run_id: null, events: [], offset: 0, running: false, status: "", aggregate: emptyRunEventAggregate() });
    setRunBlockPreview([]);
    setRunWatchlist([]);
    setRunReportSummary(null);
    setRunPromptPreview(null);
    setRunError("");
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

  const thesisProjectForJob = useCallback((jobId) => {
    const docId = `${THESIS_PREFIX}${jobId}`;
    return (projects || []).find(row => row.job_id === jobId || row.doc_id === docId) || null;
  }, [projects]);

  const loadThesisDataset = useCallback(async (jobId, opts = {}) => {
    if (!jobId) return null;
    if (!opts.silent) setLoading(true);
    const project = opts.project || thesisProjectForJob(jobId);
    const thesisParams = thesisRuntimeParams(jobId, { project });
    const [dataset, observability] = await Promise.all([
      API.getThesisDataset(jobId, thesisParams),
      API.getThesisObservability(jobId).catch(err => emptyThesisObservability(jobId, errorMessage(err))),
    ]);
    const adapted = adaptThesisReadModel(dataset);
    setThesisBaseDataset(adapted);
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
    setProjectRuntime(null);
    API.listThesisRuns()
      .then(rows => setThesisRuns(runsForJob(rows, jobId)))
      .catch(() => setThesisRuns([]));
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
  }, [thesisProjectForJob]);

  async function boot() {
    setLoading(true);
    setBootError(null);
    try {
      API.getVersion()
        .then(value => setAppVersion({ ui_version: UI_VERSION, ...(value || {}) }))
        .catch(() => setAppVersion({ ui_version: UI_VERSION, backend_version: "unknown", git_sha: "unknown" }));
      const list = await refreshProjects();
      const remembered = localStorage.getItem(STORAGE_DOC);
      const savedMode = localStorage.getItem(STORAGE_CENTER_MODE);
      const rememberedProject = list.find(p => p.doc_id === remembered);
      const chosen = (savedMode === "structure" && rememberedProject?.source !== "thesis" ? rememberedProject : null)
        || list.find(p => p.doc_id === remembered && p.status === "available")
        || list.find(p => p.status === "available")
        || list[0];
      if (!chosen) {
        setDocInfo({ doc_id: "", metadata: {}, provenance: {} });
        navigateView("project", { replace: true });
        setLoading(false);
        return;
      }
      setActiveDocId(chosen.doc_id);
      if (chosen.source === "thesis") {
        await loadThesisDataset(thesisJobId(chosen.doc_id), { project: chosen });
        navigateView("workspace", { replace: true });
      } else if (savedMode === "structure") {
        await openSourcePackage(chosen.doc_id, { replace: true });
        setLoading(false);
      } else if (chosen.status === "available") {
        await loadDataset(chosen.doc_id);
      } else {
        setDocInfo({ doc_id: chosen.doc_id, metadata: {}, provenance: {} });
        navigateView("project", { replace: true });
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
  const runtimeJobId = thesisJobId(activeDocId)
    || (projectRuntime?.prepared && projectRuntime?.project_id === activeDocId ? projectRuntime.job_id : "");

  useEffect(() => {
    if (!runtimeJobId || !selectedCallId) {
      setSelectedCallDetail(null);
      setCallDetailLoading(false);
      return;
    }
    let cancelled = false;
    setCallDetailLoading(true);
    API.getThesisObservabilityCall(runtimeJobId, selectedCallId)
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
  }, [runtimeJobId, selectedCallId]);

  const refreshThesisRuns = useCallback(async () => {
    if (!runtimeJobId) return [];
    const rows = await API.listThesisRuns();
    const scoped = runsForJob(rows, runtimeJobId);
    setThesisRuns(scoped);
    return scoped;
  }, [runtimeJobId]);

  function runFormPayload(includeToken = false) {
    const payload = {
      script: runForm.script || "run_translate",
      job_id: runtimeJobId || undefined,
      chapters: splitWords(runForm.chapters),
      configs: splitWords(runForm.configs),
      profile: runForm.profile || undefined,
      experiment: runForm.experiment || undefined,
      cache: runForm.cache || undefined,
      allow_api: !!runForm.allow_api,
    };
    if (includeToken && runPromptPreview?.confirm_token) {
      payload.confirm_token = runPromptPreview.confirm_token;
    }
    if (includeToken && runPromptPreview?.planned_run_id) {
      payload.planned_run_id = runPromptPreview.planned_run_id;
    }
    return payload;
  }

  async function previewThesisRun() {
    if (!runtimeJobId) return;
    setRunBusy(true);
    setRunError("");
    setRunPromptPreview(null);
    try {
      const params = {
        job_id: runtimeJobId,
        script: runForm.script || "run_translate",
        chapters: splitWords(runForm.chapters).join(" "),
        configs: splitWords(runForm.configs).join(" "),
        profile: runForm.profile || "",
        experiment: runForm.experiment || "",
        cache: runForm.cache || "",
      };
      const preview = await API.getThesisRunPromptPreview(params);
      setRunPromptPreview(preview);
      toast(uiText("Bản xem trước prompt đã sẵn sàng", "Prompt preview ready"), "good", uiText(`${preview.representative_prompt?.prompt_tokens_est || 0} token prompt ước tính`, `${preview.representative_prompt?.prompt_tokens_est || 0} estimated prompt tokens`));
    } catch (err) {
      const msg = errorMessage(err);
      setRunError(msg);
      toast(uiText("Xem trước prompt thất bại", "Prompt preview failed"), "bad", msg);
    } finally {
      setRunBusy(false);
    }
  }

  async function createThesisRun() {
    setRunBusy(true);
    setRunError("");
    try {
      const payload = runFormPayload(!!runForm.allow_api);
      const created = await API.createThesisRun(payload);
      setSelectedRunId(created.run_id);
      runLogOffsetRef.current = 0;
      runEventOffsetRef.current = 0;
      setSelectedRunLog({ run_id: created.run_id, log: "", offset: 0, running: true, status: created.status });
      setSelectedRunEvents({ run_id: created.run_id, events: [], offset: 0, running: true, status: created.status, aggregate: emptyRunEventAggregate() });
      await refreshThesisRuns();
      toast(uiText("Đã khởi chạy lần chạy", "Run launched"), "good", created.run_id);
    } catch (err) {
      const msg = errorMessage(err);
      setRunError(msg);
      toast(uiText("Khởi chạy thất bại", "Run launch failed"), "bad", msg);
    } finally {
      setRunBusy(false);
    }
  }

  function updateRunForm(patch) {
    setRunPromptPreview(null);
    setRunForm(form => ({ ...form, ...patch }));
  }

  function selectRun(runId) {
    setSelectedRunId(runId);
    runLogOffsetRef.current = 0;
    runEventOffsetRef.current = 0;
    setSelectedRunLog({ run_id: runId, log: "", offset: 0, running: true, status: "" });
    setSelectedRunEvents({ run_id: runId, events: [], offset: 0, running: true, status: "", aggregate: emptyRunEventAggregate() });
    setRunBlockPreview([]);
    setRunWatchlist([]);
    setRunReportSummary(null);
  }

  async function pauseRun() {
    if (!selectedRunId) return;
    try {
      await API.pauseThesisRun(selectedRunId);
      toast(uiText("Đã đặt cờ tạm dừng", "Pause requested"), "good", uiText("Lần chạy sẽ dừng ở ranh giới tầng kế tiếp", "The run will pause at the next stage boundary"));
    } catch (err) {
      toast(uiText("Tạm dừng thất bại", "Pause failed"), "bad", errorMessage(err));
    }
  }

  function cancelRun() {
    if (!selectedRunId) return;
    setModal({ kind: "cancel-run", runId: selectedRunId });
  }

  async function confirmCancelRun() {
    const runId = modal?.runId || selectedRunId;
    setModal(null);
    if (!runId) return;
    try {
      await API.cancelThesisRun(runId);
      toast(uiText("Đã gửi lệnh hủy", "Cancel requested"), "good", runId);
      await refreshThesisRuns();
    } catch (err) {
      toast(uiText("Hủy thất bại", "Cancel failed"), "bad", errorMessage(err));
    }
  }

  async function resumeRun() {
    if (!selectedRunId) return;
    setRunBusy(true);
    try {
      const estimate = await API.getThesisResumeEstimate(selectedRunId);
      setModal({ kind: "resume-run", runId: selectedRunId, estimate });
    } catch (err) {
      toast(uiText("Không lấy được ước tính tiếp tục", "Could not load resume estimate"), "bad", errorMessage(err));
    } finally {
      setRunBusy(false);
    }
  }

  function openProjectPipelineModal() {
    if (thesisJobId(activeDocId)) {
      openDichModal();
      return;
    }
    const domain = String(docInfo?.metadata?.domain || "").toLowerCase();
    const defaultProfile = domain === "technical" || domain === "d2l" ? "technical_d2l_v1" : "literary_v1";
    setModal({
      kind: "project-pipeline",
      profile: projectRuntime?.selected_profile || defaultProfile,
      chapters: chapters.map(chapter => chapter.chapter_id).filter(Boolean),
    });
  }

  function toggleProjectPipelineChapter(chapterId) {
    setModal(current => {
      if (current?.kind !== "project-pipeline") return current;
      const selected = new Set(current.chapters || []);
      if (selected.has(chapterId)) selected.delete(chapterId);
      else selected.add(chapterId);
      return { ...current, chapters: chapters.map(chapter => chapter.chapter_id).filter(id => selected.has(id)) };
    });
  }

  async function confirmProjectPreflight() {
    const pending = modal;
    if (pending?.kind !== "project-pipeline" || !(pending.chapters || []).length) return;
    setRunBusy(true);
    setRunError("");
    try {
      const runtime = await API.prepareProjectRuntime(activeDocId);
      const profile = pending.profile || "literary_v1";
      const selectedChapters = pending.chapters || [];
      const payload = {
        script: "run_translate",
        job_id: runtime.job_id,
        chapters: selectedChapters,
        configs: ["S0"],
        profile,
        experiment: "imported_project_preflight",
        allow_api: false,
      };
      const created = await API.createThesisRun(payload);
      const [observability, rows] = await Promise.all([
        API.getThesisObservability(runtime.job_id).catch(err => emptyThesisObservability(runtime.job_id, errorMessage(err))),
        API.listThesisRuns().catch(() => []),
      ]);
      setProjectRuntime({ ...runtime, selected_profile: profile });
      setThesisObservability(observability);
      setThesisRuns(runsForJob(rows, runtime.job_id));
      setRunForm({
        script: "run_translate",
        chapters: selectedChapters.join(" "),
        configs: "S0",
        profile,
        experiment: "imported_project_preflight",
        cache: "",
        allow_api: false,
      });
      setRunPromptPreview(null);
      setSelectedRunId(created.run_id);
      runLogOffsetRef.current = 0;
      runEventOffsetRef.current = 0;
      setSelectedRunLog({ run_id: created.run_id, log: "", offset: 0, running: true, status: created.status });
      setSelectedRunEvents({ run_id: created.run_id, events: [], offset: 0, running: true, status: created.status, aggregate: emptyRunEventAggregate() });
      setRunBlockPreview([]);
      setRunWatchlist([]);
      setRunReportSummary(null);
      setModal(null);
      setCenterMode("console");
      toast(uiText("Đã khởi chạy kiểm tra pipeline", "Pipeline check launched"), "good", `${runtime.job_id} · ${uiText("không gọi API", "no API calls")}`);
    } catch (err) {
      const message = errorMessage(err);
      setRunError(message);
      toast(uiText("Không thể khởi chạy pipeline", "Could not launch pipeline"), "bad", message);
    } finally {
      setRunBusy(false);
    }
  }

  function openDichModal() {
    const jobId = thesisJobId(activeDocId);
    if (!jobId) { toast(uiText("Chưa mở dataset khóa luận", "No thesis dataset open"), "bad", uiText("Chọn một dataset khóa luận trước khi dịch.", "Choose a thesis dataset before translating.")); return; }
    setModal({ kind: "dich-run", jobId });
  }

  async function confirmDich() {
    const jobId = thesisJobId(activeDocId);
    if (!jobId) return;
    setRunBusy(true);
    try {
      const estParams = {
        job_id: jobId,
        script: "run_one_button",
        chapters: (dichForm.chapters || "").trim(),
        profile: dichForm.profile || "",
        experiment: "one_button_ui",
        budget_cap_usd: String(dichForm.budget_cap_usd || 1.5),
        db: dichForm.db || "",
        with_s0: String(!!dichForm.with_s0),
      };
      const est = await API.getThesisOneButtonEstimate(estParams);
      const payload = {
        script: "run_one_button",
        job_id: jobId,
        db: dichForm.db || undefined,
        chapters: splitWords(dichForm.chapters),
        profile: dichForm.profile || undefined,
        experiment: "one_button_ui",
        budget_cap_usd: Number(dichForm.budget_cap_usd || 1.5),
        with_s0: !!dichForm.with_s0,
        allow_api: true,
        confirm_token: est.confirm_token,
        planned_run_id: est.planned_run_id,
      };
      const created = await API.createThesisRun(payload);
      setModal(null);
      setSelectedRunId(created.run_id);
      runLogOffsetRef.current = 0;
      runEventOffsetRef.current = 0;
      setSelectedRunLog({ run_id: created.run_id, log: "", offset: 0, running: true, status: created.status });
      setSelectedRunEvents({ run_id: created.run_id, events: [], offset: 0, running: true, status: created.status, aggregate: emptyRunEventAggregate() });
      setRunBlockPreview([]);
      setRunWatchlist([]);
      setRunReportSummary(null);
      setCenterMode("console");
      await refreshThesisRuns();
      toast(uiText("Đã bắt đầu dịch", "Translation started"), "good", created.run_id);
    } catch (err) {
      toast(uiText("Khởi động dịch thất bại", "Translation launch failed"), "bad", errorMessage(err));
    } finally {
      setRunBusy(false);
    }
  }

  async function confirmResumeRun() {
    const pending = modal;
    setModal(null);
    if (!pending?.runId || !pending?.estimate?.confirm_token) return;
    setRunBusy(true);
    try {
      const resumed = await API.resumeThesisRun(pending.runId, { confirm_token: pending.estimate.confirm_token });
      toast(uiText("Đã tiếp tục lần chạy", "Run resumed"), "good", `${resumed.run_id} · attempt ${resumed.attempt_index}`);
      await refreshThesisRuns();
      selectRun(resumed.run_id);
    } catch (err) {
      toast(uiText("Tiếp tục thất bại", "Resume failed"), "bad", errorMessage(err));
    } finally {
      setRunBusy(false);
    }
  }

  useEffect(() => {
    if (!selectedRunId) return undefined;
    let cancelled = false;
    let timer = null;
    async function poll() {
      try {
        const currentOffset = runLogOffsetRef.current || 0;
        const currentEventOffset = runEventOffsetRef.current || 0;
        const [result, eventResult, previewResult, watchResult, reportResult] = await Promise.all([
          API.getThesisRunLog(selectedRunId, currentOffset),
          API.getThesisRunEvents(selectedRunId, currentEventOffset, 262144).catch(() => null),
          API.getThesisRunBlockPreview(selectedRunId).catch(() => null),
          API.getThesisRunWatchlist(selectedRunId).catch(() => null),
          API.getThesisRunReportSummary(selectedRunId).catch(() => null),
        ]);
        if (cancelled) return;
        if (previewResult) setRunBlockPreview(previewResult.blocks || []);
        if (watchResult) setRunWatchlist(watchResult.watchlist || []);
        if (reportResult) setRunReportSummary(reportResult);
        runLogOffsetRef.current = result.offset || currentOffset;
        if (eventResult) runEventOffsetRef.current = eventResult.offset || currentEventOffset;
        setSelectedRunLog(prev => ({
          run_id: selectedRunId,
          log: (prev.run_id === selectedRunId ? prev.log : "") + (result.log || ""),
          offset: result.offset || currentOffset,
          running: !!result.running,
          status: result.status || "",
          exit_code: result.exit_code,
        }));
        if (eventResult) {
          const newEvents = eventResult.events || [];
          setSelectedRunEvents(prev => {
            const sameRun = prev.run_id === selectedRunId;
            return {
              run_id: selectedRunId,
              events: [
                ...(sameRun ? (prev.events || []) : []),
                ...newEvents,
              ].slice(-20000),
              aggregate: updateRunEventAggregate(sameRun ? prev.aggregate : emptyRunEventAggregate(), newEvents),
              offset: eventResult.offset || currentEventOffset,
              truncated: !!eventResult.truncated,
              partial_line: !!eventResult.partial_line,
              running: !!eventResult.running,
              status: eventResult.status || "",
              exit_code: eventResult.exit_code,
            };
          });
        }
        await refreshThesisRuns();
        const needsDrain = !!eventResult?.truncated;
        const needsPartialFollowup = !!eventResult?.partial_line;
        const pollStatus = eventResult?.status || result.status || "";
        if (needsDrain) {
          timer = setTimeout(poll, 0);
        } else if (needsPartialFollowup || !isRunTerminalStatus(pollStatus)) {
          timer = setTimeout(poll, needsPartialFollowup ? 600 : 1400);
        }
      } catch (_err) {
        if (!cancelled) timer = setTimeout(poll, 2500);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [selectedRunId, refreshThesisRuns]);

  // Entering a run surface with nothing selected: auto-pick the newest run so
  // Console and Report share the same current run (registry list is newest-first).
  useEffect(() => {
    if (!["console", "report"].includes(view) || selectedRunId || !thesisRuns.length) return;
    selectRun(thesisRuns[0].run_id);
  }, [view, selectedRunId, thesisRuns]);

  function setCenterMode(mode) {
    if (["console", "report"].includes(mode)) {
      navigateView(mode);
      return;
    }
    setCenterModeState(mode);
    localStorage.setItem(STORAGE_CENTER_MODE, mode);
  }

  function toggleLeftPanel() {
    setLeftPanelOpen(open => {
      const next = !open;
      localStorage.setItem(STORAGE_LEFT_PANEL, String(next));
      return next;
    });
  }

  function toggleRightPanel() {
    setRightPanelOpen(open => {
      const next = !open;
      localStorage.setItem(STORAGE_RIGHT_PANEL, String(next));
      return next;
    });
  }

  function toggleRightTab(tabId) {
    setRightOpenTabs([tabId]);
  }

  function openRightTab(tabId) {
    setRightOpenTabs([tabId]);
  }

  function toggleRightPanelExpanded() {
    setRightPanelExpanded(expanded => {
      const next = !expanded;
      localStorage.setItem(STORAGE_RIGHT_PANEL_EXPANDED, String(next));
      return next;
    });
  }

  function openMemory(kind) {
    const requested = kind || rightOpenTabs.find(tabId => ["glossary", "entities", "relations", "summary"].includes(tabId));
    setMemoryFocusKind(requested === "summary" ? "summaries" : (requested || "glossary"));
    setCenterMode("memory");
  }

  const touchStart = useCallback(() => {
    setDirty(true);
    savedAt.current = Date.now();
    setLastSaved(0);
  }, []);

  const touchDone = useCallback(() => {
    savedAt.current = Date.now();
    setDirty(false);
    setLastSaved(0);
  }, []);

  useEffect(() => {
    const t = setInterval(() => {
      const s = Math.round((Date.now() - savedAt.current) / 1000);
      setLastSaved(s);
    }, 4000);
    return () => clearInterval(t);
  }, []);

  function toast(msg, tone, sub) {
    const id = Math.random().toString(36).slice(2);
    setToasts(t => [...t, { id, msg, tone, sub }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 4200);
  }
  const dismiss = id => setToasts(t => t.filter(x => x.id !== id));

  async function openSourcePackage(docId = activeDocId, { replace = false } = {}) {
    const targetDocId = String(docId || "");
    if (!targetDocId || isThesisDatasetId(targetDocId)) return;
    setActiveDocId(targetDocId);
    localStorage.setItem(STORAGE_DOC, targetDocId);
    if (targetDocId !== docInfo?.doc_id || isThesisDatasetId(docInfo?.doc_id)) {
      let detail = null;
      try { detail = await API.getProject(targetDocId); } catch (_err) {}
      const projectRow = projects.find(row => row.doc_id === targetDocId) || {};
      setDocInfo({
        doc_id: targetDocId,
        metadata: detail?.metadata || projectRow.metadata || {},
        provenance: detail?.provenance || {},
      });
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
    }
    setCenterModeState("structure");
    localStorage.setItem(STORAGE_CENTER_MODE, "structure");
    navigateView("workspace", { replace });
  }

  function openProjectSource() {
    if (isThesisDatasetId(activeDocId)) {
      navigateView("workspace", { replace: true });
      setModal({ kind: "quick-import" });
      return;
    }
    navigateView("project");
  }

  async function mutate(action, { refresh = true, success, fail } = {}) {
    if (!activeDocId) return null;
    if (readOnly) {
      toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Dữ liệu pipeline SQLite chỉ có thể quan sát trong APP-A01.", "Pipeline SQLite data is observable only in APP-A01."));
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
      toast(fail || uiText("Thao tác thất bại", "Action failed"), "bad", errorMessage(err));
      return null;
    }
  }

  function queueSave(key, action) {
    if (readOnly) {
      toast(uiText("Chế độ xem", "Viewer mode"), "info", uiText("Workspace không cho sửa dataset này.", "Workspace editing is disabled for this dataset."));
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
        toast(uiText("Lưu thất bại", "Save failed"), "bad", errorMessage(err));
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

  useEffect(() => {
    const jobId = thesisJobId(activeDocId);
    if (!readOnly || !jobId || !thesisBaseDataset || !selectedId) {
      setRegistryOverlayLoading(false);
      return undefined;
    }
    const baseBlock = (thesisBaseDataset.blocks || []).find(row => row.block_id === selectedId) || thesisBaseDataset.blocks?.[0];
    if (!baseBlock) {
      setRegistryOverlayLoading(false);
      return undefined;
    }
    const params = centerMode === "memory"
      ? {}
      : (centerMode === "chapter" || centerMode === "book")
        ? { chapter_id: baseBlock.chapter_id }
        : { block_id: baseBlock.block_id };
    Object.assign(params, thesisRuntimeParams(jobId, { project: thesisProjectForJob(jobId) }));
    params.overlay_mode = "localization";
    const cacheKey = `${jobId}:${JSON.stringify(params)}`;
    if (centerMode === "memory") {
      const cachedOverlay = fullRegistryOverlayCacheRef.current.get(cacheKey);
      if (cachedOverlay) {
        const adapted = applyRegistryOverlay(thesisBaseDataset, cachedOverlay);
        setBlocks(adapted.blocks);
        setGlossary(adapted.glossary);
        setEntities(adapted.entities);
        setRegistryOverlayLoading(false);
        return undefined;
      }
      setRegistryOverlayLoading(true);
    } else {
      setRegistryOverlayLoading(false);
    }
    let cancelled = false;
    API.getThesisRegistryOverlay(jobId, params)
      .then(overlay => {
        if (cancelled) return;
        if (centerMode === "memory") fullRegistryOverlayCacheRef.current.set(cacheKey, overlay);
        const adapted = applyRegistryOverlay(thesisBaseDataset, overlay);
        setBlocks(adapted.blocks);
        setGlossary(adapted.glossary);
        setEntities(adapted.entities);
      })
      .catch(err => {
        if (!cancelled) toast(uiText("Lớp phủ registry không khả dụng", "Registry overlay unavailable"), "bad", errorMessage(err));
      })
      .finally(() => {
        if (!cancelled && centerMode === "memory") setRegistryOverlayLoading(false);
      });
    return () => { cancelled = true; };
  }, [readOnly, activeDocId, thesisBaseDataset, selectedId, centerMode, thesisProjectForJob]);

  const blockTerms = useMemo(() => block
    ? glossary.filter(term => (term.occurrences || []).some(occurrence => occurrence.block_id === block.block_id))
    : [], [glossary, block]);
  const directBlockEntityIds = useMemo(() => {
    if (!block) return new Set();
    const ids = new Set([
      block.discourse?.speaker_entity_id,
      block.discourse?.addressee_entity_id,
    ].filter(Boolean));
    entities.forEach(entity => {
      const hasMention = (entity.mentions || []).some(mention => mention.block_id === block.block_id);
      const isBoundaryOccurrence = entity.first_block_id === block.block_id || entity.latest_block_id === block.block_id;
      if (hasMention || isBoundaryOccurrence) ids.add(entity.entity_id);
    });
    return ids;
  }, [entities, block]);
  const blockRelations = useMemo(() => {
    if (!block) return [];
    return relations.filter(relation => {
      const hasBlockEvidence = (relation.evidence || []).some(evidence => [
        evidence.block_id,
        evidence.trigger_block_id,
        evidence.source_block_id,
      ].includes(block.block_id));
      const isPhaseBoundary = relation.valid_from_block_id === block.block_id
        || relation.valid_to_block_id === block.block_id;
      const hasGroundedPair = directBlockEntityIds.has(relation.source_entity_id)
        && directBlockEntityIds.has(relation.target_entity_id);
      return hasBlockEvidence || isPhaseBoundary || hasGroundedPair;
    });
  }, [relations, block, directBlockEntityIds]);
  const blockEntities = useMemo(() => {
    const ids = new Set(directBlockEntityIds);
    blockRelations.forEach(relation => {
      if (relation.source_entity_id) ids.add(relation.source_entity_id);
      if (relation.target_entity_id) ids.add(relation.target_entity_id);
    });
    return entities.filter(entity => ids.has(entity.entity_id));
  }, [entities, directBlockEntityIds, blockRelations]);
  const summary = useMemo(() => block ? (summaries.find(s => s.chapter_id === block.chapter_id) || { doc_id: docInfo?.doc_id, chapter_id: block.chapter_id, summary_source: "", source: "" }) : null, [summaries, block, docInfo]);
  const contextScope = useMemo(() => {
    if (!block) return { kind: "block", label: "Current block", blockIds: new Set() };
    if (centerMode === "book") {
      return {
        kind: "book",
        label: "Current book",
        blockIds: new Set(blocks.map(row => row.block_id)),
      };
    }
    if (centerMode === "chapter") {
      const title = activeChapter?.title || activeChapter?.chapter_title || block.chapter_id;
      return {
        kind: "chapter",
        label: title || "Current chapter",
        blockIds: new Set(chapterBlocks.map(row => row.block_id)),
      };
    }
    return {
      kind: "block",
      label: block.block_id,
      blockIds: new Set([block.block_id]),
    };
  }, [block, centerMode, blocks, activeChapter, chapterBlocks]);
  const contextTerms = useMemo(() => glossary.filter(term => (
    (term.occurrences || []).some(occurrence => contextScope.blockIds.has(occurrence.block_id))
  )), [glossary, contextScope]);
  const directContextEntityIds = useMemo(() => {
    const ids = new Set();
    blocks.forEach(row => {
      if (!contextScope.blockIds.has(row.block_id)) return;
      if (row.discourse?.speaker_entity_id) ids.add(row.discourse.speaker_entity_id);
      if (row.discourse?.addressee_entity_id) ids.add(row.discourse.addressee_entity_id);
    });
    entities.forEach(entity => {
      const hasMention = (entity.mentions || []).some(mention => contextScope.blockIds.has(mention.block_id));
      const hasBoundary = contextScope.blockIds.has(entity.first_block_id) || contextScope.blockIds.has(entity.latest_block_id);
      if (hasMention || hasBoundary) ids.add(entity.entity_id);
    });
    return ids;
  }, [blocks, entities, contextScope]);
  const contextRelations = useMemo(() => relations.filter(relation => {
    const hasEvidence = (relation.evidence || []).some(evidence => (
      contextScope.blockIds.has(evidence.block_id)
      || contextScope.blockIds.has(evidence.trigger_block_id)
      || contextScope.blockIds.has(evidence.source_block_id)
    ));
    const hasBoundary = contextScope.blockIds.has(relation.valid_from_block_id)
      || contextScope.blockIds.has(relation.valid_to_block_id);
    const hasGroundedPair = directContextEntityIds.has(relation.source_entity_id)
      && directContextEntityIds.has(relation.target_entity_id);
    return hasEvidence || hasBoundary || hasGroundedPair;
  }), [relations, contextScope, directContextEntityIds]);
  const contextEntities = useMemo(() => {
    const ids = new Set(directContextEntityIds);
    contextRelations.forEach(relation => {
      if (relation.source_entity_id) ids.add(relation.source_entity_id);
      if (relation.target_entity_id) ids.add(relation.target_entity_id);
    });
    return entities.filter(entity => ids.has(entity.entity_id));
  }, [entities, directContextEntityIds, contextRelations]);
  const contextSummaries = useMemo(() => {
    if (!block) return [];
    if (contextScope.kind === "book") return summaries;
    return summaries.filter(item => item.chapter_id === block.chapter_id);
  }, [summaries, block, contextScope]);
  const currentMemory = useMemo(() => ({
    glossary: contextTerms,
    entities: contextEntities,
    relations: contextRelations,
    summaries: contextSummaries,
  }), [contextTerms, contextEntities, contextRelations, contextSummaries]);
  const projectMemory = useMemo(() => ({
    glossary,
    entities,
    relations,
    summaries,
  }), [glossary, entities, relations, summaries]);

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
  const scopedCount = (localCount, totalCount) => totalCount ? `${localCount}/${totalCount}` : null;
  const rpCounts = {
    glossary: { text: scopedCount(contextTerms.length, glossary.length) },
    entities: { text: scopedCount(contextEntities.length, entities.length) },
    relations: { text: scopedCount(contextRelations.length, relations.length) },
    summary: { text: contextSummaries.some(item => item.summary_source) ? null : "empty", tone: contextSummaries.some(item => item.summary_source) ? "" : "warn" },
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
      toast(uiText(`Mục chưa duyệt tiếp theo: ${next.block_id}`, `Next unreviewed: ${next.block_id}`), "info");
    } else {
      toast(uiText("Tất cả block đã được duyệt", "All blocks are reviewed"), "good");
    }
  }

  async function selectProject(docId) {
    const chosen = projects.find(p => p.doc_id === docId);
    setActiveDocId(docId);
    localStorage.setItem(STORAGE_DOC, docId);
    const thesisId = thesisJobId(docId);
    if (thesisId) {
      await loadThesisDataset(thesisId, { project: chosen || null });
      navigateView("workspace");
    } else if (centerMode === "structure") {
      await openSourcePackage(docId);
    } else if (chosen?.status === "available") {
      await loadDataset(docId);
      navigateView("workspace");
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
      navigateView("project");
    }
  }

  function patchDoc(patch) {
    if (readOnly) {
      toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Chỉnh metadata thuộc write lane sau với audit log rõ ràng.", "Metadata edits belong to a later write lane with explicit audit logs."));
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

  async function createProject(docId, metadata, options = {}) {
    if (isThesisDatasetId(docId)) {
      toast(uiText("ID dự án cục bộ không hợp lệ", "Invalid local project id"), "bad", uiText("Namespace thesis: được dành cho dataset runtime chỉ đọc.", "The thesis: namespace is reserved for read-only runtime datasets."));
      return null;
    }
    try {
      const result = await API.createProject({ doc_id: docId, metadata });
      await refreshProjects();
      if (options.activate !== false) {
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
      }
      toast(uiText("Đã tạo dự án", "Project created"), "good", result.doc_id);
      return result;
    } catch (err) {
      toast(uiText("Tạo dự án thất bại", "Create project failed"), "bad", errorMessage(err));
      return null;
    }
  }

  async function updateProjectSettings(docId, patch) {
    if (!docId) return null;
    if (isThesisDatasetId(docId)) {
      toast(uiText("Dataset khóa luận chỉ đọc", "Read-only thesis dataset"), "info", uiText("Chỉ có thể đổi cài đặt của dự án nguồn cục bộ.", "Project settings can only be changed for local source projects."));
      return null;
    }
    try {
      const result = await API.patchProject(docId, { ...patch, user: currentUser() });
      await refreshProjects();
      toast(uiText("Đã cập nhật dự án", "Project updated"), "good");
      return result;
    } catch (err) {
      toast(uiText("Cập nhật dự án thất bại", "Update project failed"), "bad", errorMessage(err));
      return null;
    }
  }

  async function deleteProjectById(docId, confirmDocId) {
    if (!docId) return null;
    if (isThesisDatasetId(docId)) {
      toast(uiText("Dataset khóa luận chỉ đọc", "Read-only thesis dataset"), "info", uiText("Không thể xóa dataset runtime khóa luận từ màn hình dự án nguồn.", "Thesis runtime datasets cannot be deleted from the source-project screen."));
      return null;
    }
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
        navigateView("project");
      }
      toast(uiText("Đã xóa dự án", "Project deleted"), "good", result.doc_id);
      return result;
    } catch (err) {
      toast(uiText("Xóa dự án thất bại", "Delete project failed"), "bad", errorMessage(err));
      return null;
    }
  }

  async function uploadSource(file, overwrite, docIdOverride = "") {
    const targetDocId = docIdOverride || activeDocId;
    if (!targetDocId || !file) return null;
    if (isThesisDatasetId(targetDocId)) {
      toast(uiText("Tải lên bị chặn", "Upload blocked"), "bad", uiText("Hãy tạo dự án cục bộ trước khi tải lên; dataset khóa luận là chỉ đọc.", "Create a local project before uploading; thesis datasets are read-only."));
      return null;
    }
    try {
      const result = await API.uploadSource(targetDocId, file, overwrite);
      await refreshProjects();
      toast(uiText("Đã tải nguồn", "Source uploaded"), "good", result.filename);
      return result;
    } catch (err) {
      toast(uiText("Tải lên thất bại", "Upload failed"), "bad", errorMessage(err));
      return null;
    }
  }

  async function normalizeManagedSourcePackage(docIdOverride = "") {
    const targetDocId = docIdOverride || activeDocId;
    if (!targetDocId || isThesisDatasetId(targetDocId)) return null;
    try {
      const result = await API.normalizeSourcePackage(targetDocId);
      await refreshProjects();
      toast(result?.reused ? uiText("Source package đã được xác nhận lại", "Source package reconfirmed") : uiText("Đã chuẩn hóa source package", "Source package normalized"), "good", targetDocId);
      return result;
    } catch (err) {
      const detail = firstError(err);
      toast(uiText("Chuẩn hóa thất bại", "Normalization failed"), "bad", [detail.code, errorMessage(err)].filter(Boolean).join(" · "));
      return null;
    }
  }

  async function openPreparedRunControl(preparedRuntime) {
    const jobId = String(preparedRuntime?.job_id || "");
    if (!jobId) return;
    setRunBusy(true);
    try {
      const list = await refreshProjects();
      const project = list.find(row => row.job_id === jobId || row.doc_id === `${THESIS_PREFIX}${jobId}`) || null;
      await loadThesisDataset(jobId, { project });
      setCenterModeState("book");
      localStorage.setItem(STORAGE_CENTER_MODE, "book");
      navigateView("workspace");
      setModal({ kind: "dich-run", jobId });
    } catch (err) {
      toast(uiText("Không mở được Điều khiển chạy", "Could not open Run Control"), "bad", errorMessage(err));
    } finally {
      setRunBusy(false);
    }
  }

  async function runExtract(overwrite, docIdOverride = "") {
    const targetDocId = docIdOverride || activeDocId;
    if (!targetDocId) return null;
    if (isThesisDatasetId(targetDocId)) {
      toast(uiText("Trích xuất bị chặn", "Extraction blocked"), "bad", uiText("Chỉ có thể ghi kết quả trích xuất vào dự án nguồn cục bộ.", "Extraction can only write to a local source project."));
      return null;
    }
    try {
      const job = await API.extract(targetDocId, { overwrite: !!overwrite, user: currentUser() });
      await refreshProjects();
      await loadDataset(targetDocId, { silent: true });
      setActiveDocId(targetDocId);
      localStorage.setItem(STORAGE_DOC, targetDocId);
      navigateView("workspace");
      toast(uiText("Trích xuất hoàn tất", "Extraction complete"), "good", `${job.document?.blocks || 0} block · ${job.document?.chapters || 0} ${uiText("chương", "chapters")}`);
      return job;
    } catch (err) {
      toast(uiText("Trích xuất thất bại", "Extraction failed"), "bad", errorMessage(err));
      return null;
    }
  }

  function findBlock(blockId) {
    return blocks.find(b => b.block_id === blockId) || null;
  }

  function changeType(t, blockId = selectedId) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể đổi loại block trong SQLite read-model.", "Block type changes are disabled for SQLite read-models."));
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    setBlocks(bs => bs.map(b => b.block_id === target.block_id ? { ...b, block_type: t } : b));
    mutate(() => API.patchBlock(activeDocId, target.block_id, { block_type: t, user: currentUser() }), { success: uiText(`Đã đổi block_type → ${t}`, `block_type → ${t}`) });
  }

  function toggleOpening(blockId = selectedId) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể đổi mở đầu chương trong SQLite read-model.", "Chapter opening changes are disabled for SQLite read-models."));
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    const next = !target.is_chapter_opening;
    setBlocks(bs => bs.map(b => b.block_id === target.block_id ? { ...b, is_chapter_opening: next } : b));
    mutate(() => API.patchBlock(activeDocId, target.block_id, { is_chapter_opening: next, user: currentUser() }));
  }

  function toggleFlag(f, blockId = selectedId) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể đổi cờ chất lượng trong SQLite read-model.", "Quality flags are disabled for SQLite read-models."));
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
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi trạng thái duyệt trong SQLite read-model.", "Review writes are disabled for SQLite read-models."));
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    const next = !review.blocks?.[target.block_id]?.reviewed;
    mutate(() => API.patchReview(activeDocId, target.block_id, { reviewed: next, reviewed_by: currentUser(), user: currentUser() }), {
      success: next ? uiText(`${target.block_id} đã duyệt`, `${target.block_id} marked reviewed`) : uiText(`${target.block_id} bỏ trạng thái duyệt`, `${target.block_id} marked unreviewed`),
    });
  }

  async function commitClean(blockId, text) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể sửa văn bản sạch trong SQLite read-model.", "Clean text edits are disabled for SQLite read-models."));
    const target = findBlock(blockId);
    if (!target) return;
    setSelectedId(target.block_id);
    setBlocks(bs => bs.map(b => b.block_id === target.block_id ? { ...b, clean_text: text } : b));
    setEditing(false);
    const result = await mutate(() => API.patchBlock(activeDocId, target.block_id, { clean_text: text, user: currentUser() }), { refresh: true });
    const broke = result?.stale_spans?.length || 0;
    const relocated = result?.relocated_count || 0;
    if (broke > 0 && relocated > 0) {
      toast(uiText(`Đã lưu văn bản sạch · ${relocated} span tự định vị lại, ${broke} cần gắn thẻ lại`, `Clean text saved · ${relocated} span${relocated > 1 ? "s" : ""} auto-relocated, ${broke} need re-tag`), "bad", uiText("Chỉ gắn lại các span stale được đánh dấu.", "Re-tag only the highlighted stale span(s)."));
    } else if (broke > 0) {
      toast(uiText(`Đã lưu văn bản sạch · ${broke} span chú giải không còn khớp`, `Clean text saved · ${broke} annotation span${broke > 1 ? "s" : ""} no longer match`), "bad", uiText("Gắn thẻ lại từ panel bên phải để xóa cảnh báo.", "Re-tag from the right panel to clear the warning."));
    } else if (relocated > 0) {
      toast(uiText(`Đã lưu văn bản sạch · ${relocated} span tự định vị lại`, `Clean text saved · ${relocated} span${relocated > 1 ? "s" : ""} auto-relocated`), "good");
    } else {
      toast(uiText("Đã lưu văn bản sạch", "Clean text saved"), "good");
    }
  }

  async function addGlossary(blockId, sel) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi thuật ngữ trong SQLite read-model.", "Glossary writes are disabled for SQLite read-models."));
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
    }), { success: uiText(`Đã thêm lần xuất hiện thuật ngữ "${sel.text.trim()}"`, `Added glossary occurrence "${sel.text.trim()}"`), fail: uiText("Thêm thuật ngữ thất bại", "Add glossary failed") });
    if (result) toast(uiText("Hãy đặt expected_target trong tab Thuật ngữ.", "Set expected_target in the Glossary tab."), "info");
  }
  async function addEntity(blockId, sel) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi thực thể trong SQLite read-model.", "Entity writes are disabled for SQLite read-models."));
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
    }), { success: uiText(`Đã thêm lượt nhắc thực thể "${sel.text.trim()}"`, `Added entity mention "${sel.text.trim()}"`), fail: uiText("Thêm thực thể thất bại", "Add entity failed") });
    if (result) toast(uiText("Hãy đặt canonical_target và quy tắc đại từ.", "Set canonical_target and pronoun policy."), "info");
  }

  function updateTerm(termId, patch) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Runtime memory là bất biến trong APP-A01.", "Runtime memory is immutable in APP-A01."));
    setGlossary(gs => gs.map(t => t.term_id === termId ? { ...t, ...patch } : t));
    queueSave(`term:${termId}:${Object.keys(patch).join(",")}`, () => API.patchGlossary(activeDocId, termId, { ...patch, user: currentUser() }));
  }
  function updateEntity(entityId, patch) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Runtime memory là bất biến trong APP-A01.", "Runtime memory is immutable in APP-A01."));
    setEntities(es => es.map(e => e.entity_id === entityId ? { ...e, ...patch } : e));
    queueSave(`entity:${entityId}:${Object.keys(patch).join(",")}`, () => API.patchEntity(activeDocId, entityId, { ...patch, user: currentUser() }));
  }
  async function createRelation(payload) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi quan hệ trong SQLite read-model.", "Relation writes are disabled for SQLite read-models."));
    if (!activeDocId) return null;
    openRightTab("relations");
    const result = await mutate(() => API.createRelation(activeDocId, { ...payload, user: currentUser() }), {
      success: uiText("Đã thêm quan hệ", "Relation added"),
      fail: uiText("Thêm quan hệ thất bại", "Add relation failed"),
    });
    return result;
  }
  function updateRelation(relationId, patch) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi quan hệ trong SQLite read-model.", "Relation writes are disabled for SQLite read-models."));
    const { relation_id, doc_id, ...apiPatch } = patch || {};
    setRelations(rs => rs.map(r => r.relation_id === relationId ? { ...r, ...apiPatch } : r));
    const fields = Object.keys(apiPatch).join(",") || "save";
    queueSave(`relation:${relationId}:${fields}`, () => API.patchRelation(activeDocId, relationId, { ...apiPatch, user: currentUser() }));
  }
  function updateSummary(chapterId, patch) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi tóm tắt trong SQLite read-model.", "Summary writes are disabled for SQLite read-models."));
    if (!chapterId) return;
    setSummaries(ss => {
      const exists = ss.some(s => s.chapter_id === chapterId);
      if (exists) return ss.map(s => s.chapter_id === chapterId ? { ...s, ...patch } : s);
      return [...ss, { doc_id: docInfo?.doc_id, chapter_id: chapterId, ...patch }];
    });
    queueSave(`summary:${chapterId}:${Object.keys(patch).join(",")}`, () => API.patchSummary(activeDocId, chapterId, { ...patch, user: currentUser() }));
  }
  function updateBlockNotes(blockId, patch) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi chú block trong SQLite read-model.", "Block notes are disabled for SQLite read-models."));
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
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi reference trong SQLite read-model.", "Reference writes are disabled for SQLite read-models."));
    setReferences(rs => rs.map(r => r.reference_id === referenceId ? { ...r, ...patch, canonical: false } : r));
  }
  function updateDiscourse(patch) {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể ghi discourse trong SQLite read-model.", "Discourse writes are disabled for SQLite read-models."));
    if (!block) return;
    const discourse = { ...(block.discourse || {}), ...patch };
    setBlocks(bs => bs.map(b => b.block_id === block.block_id ? { ...b, discourse } : b));
    mutate(() => API.patchBlock(activeDocId, block.block_id, { discourse, user: currentUser() }), { success: uiText("Đã lưu discourse", "Discourse saved") });
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
    }), { success: uiText("Đã lưu bản nháp reference", "Reference draft saved") });
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
    }), { success: uiText("Đã lưu bản nháp reference", "Reference draft saved"), fail: uiText("Lưu bản nháp reference thất bại", "Reference draft failed") });
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
    }), { success: uiText("Đã đánh dấu reference là đã duyệt", "Reference marked reviewed"), fail: uiText("Không thể duyệt reference", "Reference cannot be reviewed") });
  }
  function lockReference(referenceId) {
    mutate(() => API.lockReference(activeDocId, referenceId, { user: currentUser() }), { success: uiText("Đã khóa reference", "Reference locked"), fail: uiText("Chỉ có thể khóa reference đã duyệt", "Only reviewed references can be locked") });
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
      toast(uiText(`Đã xóa thuật ngữ "${term.source_term}"`, `Deleted term "${term.source_term}"`), "good", uiText(`Đã xóa ${result.removed_occurrences || 0} lần xuất hiện`, `${result.removed_occurrences || 0} occurrence(s) removed`));
    } catch (err) {
      touchDone();
      const first = firstError(err);
      setModal({ kind: "delete-blocked", title: uiText("Không thể xóa thuật ngữ", "Cannot delete term"), message: errorMessage(err), references: first.references || [] });
      toast(uiText("Không thể xóa thuật ngữ", "Cannot delete term"), "bad", errorMessage(err));
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
      toast(uiText(`Đã xóa thực thể "${entity.canonical_source || entity.entity_id}"`, `Deleted entity "${entity.canonical_source || entity.entity_id}"`), "good", uiText(`Đã xóa ${result.removed_mentions || 0} lượt nhắc`, `${result.removed_mentions || 0} mention(s) removed`));
    } catch (err) {
      touchDone();
      const first = firstError(err);
      setModal({ kind: "delete-blocked", title: uiText("Không thể xóa thực thể", "Cannot delete entity"), message: errorMessage(err), references: first.references || [] });
      toast(uiText("Không thể xóa thực thể", "Cannot delete entity"), "bad", errorMessage(err));
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
      toast(uiText(`Đã xóa quan hệ ${relation.relation_id}`, `Deleted relation ${relation.relation_id}`), "good");
    } catch (err) {
      touchDone();
      setModal({ kind: "delete-blocked", title: uiText("Không thể xóa quan hệ", "Cannot delete relation"), message: errorMessage(err), references: [] });
      toast(uiText("Không thể xóa quan hệ", "Cannot delete relation"), "bad", errorMessage(err));
    }
  }

  async function runValidate() {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Validate dành cho workspace JSONL AI-LAB; chấm điểm/báo cáo thuộc APP-D01.", "Validation is for AI-LAB JSONL workspaces; scoring/reporting is APP-D01."));
    openRightTab("validate");
    try {
      const report = await API.validate(activeDocId, { user: currentUser() });
      const items = normalizeErrors(report);
      setErrors(items);
      toast(uiText(`Validate: ${report.errors?.length || 0} lỗi, ${report.warnings?.length || 0} cảnh báo`, `Validation: ${report.errors?.length || 0} error${(report.errors?.length || 0) !== 1 ? "s" : ""}, ${report.warnings?.length || 0} warning${(report.warnings?.length || 0) !== 1 ? "s" : ""}`), report.ok ? "good" : "bad");
    } catch (err) {
      setErrors((err.errors || []).map(e => ({ severity: "error", ...e })));
      toast(uiText("Validate thất bại", "Validation failed"), "bad", errorMessage(err));
    }
  }

  async function migrateSchema() {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể nâng schema cho SQLite read-model.", "Schema migration is disabled for SQLite read-models."));
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
      toast(ok ? uiText("Đã nâng schema lên 1.5", "Schema migrated to 1.5") : uiText("Đã nâng schema nhưng còn lỗi validate", "Schema migrated with validation issues"), ok ? "good" : "bad", actions || uiText("Đã cập nhật file dự án.", "Project files updated."));
    } catch (err) {
      setErrors((err.errors || []).map(e => ({ severity: "error", ...e })));
      touchDone();
      toast(uiText("Nâng schema thất bại", "Schema migration failed"), "bad", errorMessage(err));
    } finally {
      setSchemaMigrating(false);
    }
  }

  function jumpTo(e) { if (e.block_id) { selectBlock(e.block_id); toast(uiText(`Đã chuyển tới ${e.block_id}`, `Jumped to ${e.block_id}`), "info"); } }

  const focusedTermMeta = useMemo(() => {
    if (!focusedTermId) return null;
    const term = glossary.find(t => t.term_id === focusedTermId || t.glossary_id === focusedTermId);
    if (!term) return { id: focusedTermId, source: focusedTermId, target: focusedTermSurface || "", count: focusedTermCount };
    return {
      id: focusedTermId,
      source: term.source_term || focusedTermId,
      target: focusedTermSurface || term.expected_target || "",
      count: focusedTermCount,
    };
  }, [focusedTermId, glossary, focusedTermCount, focusedTermSurface]);

  const syncFocusedTermDom = useCallback((id = focusedTermId) => {
    if (!id) {
      setFocusedTermCount(0);
      setFocusedTermIndex(0);
      return [];
    }
    const rows = Array.from(document.querySelectorAll(`[data-focus-id="${cssAttrEscape(id)}"]`));
    setFocusedTermCount(rows.length);
    if (rows.length && focusedTermIndex >= rows.length) setFocusedTermIndex(0);
    return rows;
  }, [focusedTermId, focusedTermIndex]);

  const clearFocusedTerm = useCallback(() => {
    setFocusedTermId(null);
    setFocusedTermIndex(0);
    setFocusedTermCount(0);
    setFocusedTermSurface("");
  }, []);

  const focusTermById = useCallback((termId, element = null, { toggle = true } = {}) => {
    const id = String(termId || "");
    if (!id) return;
    if (toggle && focusedTermId === id) {
      clearFocusedTerm();
      return;
    }
    setFocusedTermId(id);
    window.requestAnimationFrame(() => {
      const rows = Array.from(document.querySelectorAll(`[data-focus-id="${cssAttrEscape(id)}"]`));
      const index = element ? Math.max(0, rows.indexOf(element)) : 0;
      const targetRow = rows.find(row => row.dataset.focusTarget === "1");
      const clickedSurface = element?.dataset?.focusTarget === "1" ? element.dataset.focusSurface : "";
      const displaySurface = clickedSurface || targetRow?.dataset?.focusSurface || "";
      setFocusedTermCount(rows.length);
      setFocusedTermIndex(index);
      setFocusedTermSurface(displaySurface);
      const target = rows[index] || rows[0];
      if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }, [clearFocusedTerm, focusedTermId]);

  const focusSpan = useCallback((span, element) => {
    const id = span?.id || span?.term_id || span?.glossary_id;
    if (id) focusTermById(id, element);
  }, [focusTermById]);

  const jumpFocusedTerm = useCallback((delta) => {
    if (!focusedTermId) return;
    const rows = syncFocusedTermDom(focusedTermId);
    if (!rows.length) return;
    const next = (focusedTermIndex + delta + rows.length) % rows.length;
    setFocusedTermIndex(next);
    rows[next].scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focusedTermId, focusedTermIndex, syncFocusedTermDom]);

  useEffect(() => {
    if (!focusedTermId) return undefined;
    const handle = window.setTimeout(() => syncFocusedTermDom(focusedTermId), 0);
    return () => window.clearTimeout(handle);
  }, [focusedTermId, blocks, centerMode, selectedId, syncFocusedTermDom]);

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
    toast(uiText("Đã xuất báo cáo QC", "QC report exported"), "good", `${safeFilePart(report.doc_id)}_qc_report.json`);
  }
  function exportPreviewRun() {
    const run = currentPreviewRun?.run;
    if (!run) {
      toast(uiText("Chưa tải bản chạy xem trước", "No preview run loaded"), "bad", uiText("Mở Xem trước và chọn một lần chạy trước.", "Open Preview and select a run first."));
      return;
    }
    const chapterId = run.chapter_id || currentPreviewRun.chapter_id || "chapter";
    const payload = {
      kind: "translation_preview_export",
      exported_at: new Date().toISOString(),
      notice: "Preview artifact. Promotion into manual_reference_subset is disabled.",
      run,
    };
    const filename = `${safeFilePart(run.doc_id || docInfo?.doc_id)}_${safeFilePart(chapterId)}_${safeFilePart(run.run_id)}_preview.json`;
    downloadAppJsonFile(filename, payload);
    toast(uiText("Đã xuất bản dịch xem trước", "Translation preview exported"), "good", filename);
  }
  async function exportDatasetPackage() {
    const suggestedName = `${safeFilePart(docInfo?.doc_id || activeDocId)}_dataset_package.zip`;
    const saveHandle = await pickZipSaveHandle(suggestedName);
    if (saveHandle === "aborted") {
      toast(uiText("Đã hủy xuất", "Export cancelled"), "info");
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
      toast(uiText("Đã xuất gói dataset", "Exported dataset package"), "good", uiText(`${filename} · đã lưu trong exports của dự án · kèm QC`, `${filename} · saved in project exports · QC included`));
    } catch (err) {
      touchDone();
      toast(uiText("Xuất gói dataset thất bại", "Dataset package export failed"), "bad", errorMessage(err));
    }
  }
  async function exportDatasetWithPreviews() {
    const suggestedName = `${safeFilePart(docInfo?.doc_id || activeDocId)}_dataset_plus_previews.zip`;
    const saveHandle = await pickZipSaveHandle(suggestedName);
    if (saveHandle === "aborted") {
      toast(uiText("Đã hủy xuất", "Export cancelled"), "info");
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
        uiText("Đã xuất dataset + bản xem trước", "Exported dataset + previews"),
        "good",
        uiText(`${filename} · đã lưu trong exports của dự án · ${counts.runs || 0} lần chạy, ${counts.inputs || 0} input`, `${filename} · saved in project exports · runs ${counts.runs || 0}, inputs ${counts.inputs || 0}`)
      );
    } catch (err) {
      touchDone();
      toast(uiText("Xuất dataset + bản xem trước thất bại", "Dataset + preview export failed"), "bad", errorMessage(err));
    }
  }
  function doExport(kind = "package") {
    if (readOnly && kind !== "qc") {
      return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Xuất gói dataset thuộc nhánh AI-LAB; hãy dùng xuất QC cho read-model này.", "Dataset package export stays on the AI-LAB lane; use QC export for this read-model."));
    }
    if (kind === "qc") return exportQcReport();
    if (kind === "preview") return exportPreviewRun();
    if (kind === "dataset-previews") return exportDatasetWithPreviews();
    return exportDatasetPackage();
  }
  function doFreeze() {
    if (readOnly) return toast(uiText("Chế độ khóa luận chỉ đọc", "Read-only thesis view"), "info", uiText("Không thể đóng băng pipeline SQLite read-model.", "Freeze is disabled for pipeline SQLite read-models."));
    setModal({ kind: "freeze" });
  }

  async function runUndo() {
    if (!historyState.can_undo || dirty) return;
    const result = await mutate(() => API.undo(activeDocId, { user: currentUser() }), { refresh: false, fail: uiText("Hoàn tác thất bại", "Undo failed") });
    if (!result) return;
    await loadDataset(activeDocId, { silent: true });
    const target = result.event?.target || {};
    if (target.block_id) setSelectedId(target.block_id);
    toast(uiText(`Hoàn tác: ${result.event?.label || "thay đổi gần nhất"}`, `Undo: ${result.event?.label || "last change"}`), "good");
  }

  async function runRedo() {
    if (!historyState.can_redo || dirty) return;
    const result = await mutate(() => API.redo(activeDocId, { user: currentUser() }), { refresh: false, fail: uiText("Làm lại thất bại", "Redo failed") });
    if (!result) return;
    await loadDataset(activeDocId, { silent: true });
    const target = result.event?.target || {};
    if (target.block_id) setSelectedId(target.block_id);
    toast(uiText(`Làm lại: ${result.event?.label || "thay đổi vừa hoàn tác"}`, `Redo: ${result.event?.label || "last undone change"}`), "good");
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
      toast(uiText("Đã xuất gói hiện tại", "Exported current package"), "good", result.zip || result.path || uiText("Đã tạo bản xuất.", "Export created."));
    }
  }
  async function confirmFreeze() {
    try {
      const result = await API.freezeProject(activeDocId, { user: currentUser() });
      setModal(null);
      await loadDataset(activeDocId, { silent: true });
      toast(uiText("Đã đóng băng snapshot dataset", "Dataset snapshot frozen"), "good", `${result.version || uiText("đã đánh phiên bản", "versioned")} · ${result.zip || ""}`);
    } catch (err) {
      const first = err.errors?.[0] || {};
      setModal({ kind: "freeze", serverReasons: first.reasons || [errorMessage(err)] });
      toast(uiText("Không thể đóng băng", "Freeze blocked"), "bad", (first.reasons || []).join("; ") || errorMessage(err));
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
      if (ev.key === "Escape" && focusedTermId) { ev.preventDefault(); clearFocusedTerm(); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const quickImportDialog = modal?.kind === "quick-import" && (
    <QuickImportModal
      projects={projects}
      onClose={() => setModal(null)}
      onCreateProject={createProject}
      onUploadSource={uploadSource}
      onNormalize={normalizeManagedSourcePackage}
      onOpenStructure={async docId => {
        setModal(null);
        await openSourcePackage(docId);
      }}
      onOpenAdvanced={isThesisDatasetId(activeDocId) ? null : () => {
        setModal(null);
        navigateView("project");
      }}
    />
  );

  if (loading) {
    return <StartupState locale={uiLocale} onLocaleChange={setUiLocale} title={uiText("Đang tải dữ liệu backend", "Loading backend dataset")} message={uiText("Đang kết nối tới {url}...", "Connecting to {url}...", { url: API.baseUrl })} />;
  }
  if (bootError) {
    return (
      <>
        <StartupState locale={uiLocale} onLocaleChange={setUiLocale} title={uiText("Backend đang ngoại tuyến", "Backend offline")} message={uiText("UI không thể kết nối Flask backend. Hãy chạy backend ở port 5000 rồi thử lại.", "The UI could not reach the Flask backend. Start the backend on port 5000, then retry.")} secondary={bootError} action={uiText("Thử lại", "Retry")} onAction={boot} />
        <Toasts items={toasts} onDismiss={dismiss} />
      </>
    );
  }

  const runSurfaceView = view === "console" || view === "report";
  const activeCenterMode = runSurfaceView ? view : centerMode;
  const sourcePackageOverlay = window.__SOURCE_PACKAGE_UI_DEV__ === true
    ? window.SOURCE_PACKAGE_UI_FIXTURE?.overlay || null
    : null;

  if (view === "project" && !isThesisDatasetId(activeDocId)) {
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
          onBack={() => navigateView("workspace")}
          onOpenStructure={openSourcePackage}
          readOnly={readOnly}
          locale={uiLocale}
          onLocaleChange={setUiLocale}
        />
        <Toasts items={toasts} onDismiss={dismiss} />
      </>
    );
  }

  if ((!block || !docInfo) && centerMode !== "structure") {
    return (
      <>
        <StartupState locale={uiLocale} onLocaleChange={setUiLocale} title={uiText("Chưa có tài liệu", "No document yet")} message={uiText("Nhập TXT, EPUB, Markdown, HTML hoặc PDF để tạo managed source package.", "Import TXT, EPUB, Markdown, HTML, or PDF to create a managed source package.")}
          action={uiText("Nhập tài liệu", "Import document")} onAction={() => setModal({ kind: "quick-import" })}
          secondaryAction={uiText("Project / Nguồn", "Project / Source")} onSecondaryAction={openProjectSource} />
        <Toasts items={toasts} onDismiss={dismiss} />
        {quickImportDialog}
      </>
    );
  }

  return (
    <div className={"app" + (runSurfaceView ? " app--console" : "") + (view === "report" ? " app--report" : "") + (activeCenterMode === "structure" ? " app--structure" : "")}>
      {!runSurfaceView && <TopBar docId={docInfo?.doc_id || activeDocId} dirty={dirty} lastSaved={lastSaved}
        projects={projects} mode={centerMode} onModeChange={setCenterMode}
        onSelectProject={selectProject} onOpenProjectSource={openProjectSource} onQuickImport={() => setModal({ kind: "quick-import" })}
        leftPanelOpen={leftPanelOpen} rightPanelOpen={rightPanelOpen}
        onToggleLeftPanel={toggleLeftPanel} onToggleRightPanel={toggleRightPanel}
        onValidate={runValidate} onExportOption={doExport} onFreeze={doFreeze}
        onUndo={runUndo} onRedo={runRedo} history={historyState}
        freezeReady={freezeReady} freezeReasons={freezeReasons} previewReadOnly={["preview", "structure"].includes(centerMode) || readOnly} canExportPreview={!!currentPreviewRun?.run}
        appVersion={appVersion} locale={uiLocale} onLocaleChange={setUiLocale} />}
      <div className={["workspace", runSurfaceView ? "workspace--console" : "", view === "report" ? "workspace--report" : "", activeCenterMode === "memory" ? "workspace--memory" : "", activeCenterMode === "structure" ? "workspace--structure" : "", (!leftPanelOpen || activeCenterMode === "structure") ? "workspace--no-left" : "", (!rightPanelOpen || ["memory", "structure"].includes(activeCenterMode)) ? "workspace--no-right" : "", rightPanelOpen && rightPanelExpanded && !["memory", "structure"].includes(activeCenterMode) ? "workspace--right-expanded" : ""].filter(Boolean).join(" ")}>
        {!runSurfaceView && activeCenterMode !== "structure" && leftPanelOpen && <LeftSidebar docInfo={docInfo} blocks={visibleBlocks} chapters={chapters} review={review}
          annoSet={annoSet} selectedId={selectedId} onSelect={selectBlock}
          filters={filters} onToggleFilter={toggleFilter} counts={filterCounts} total={blocks.length}
          errors={errors} />}
        {activeCenterMode === "structure" ? (
          <SourcePackageWorkspace
            docId={activeDocId}
            user={currentUser()}
            api={API}
            publicationOverlay={sourcePackageOverlay}
            onOpenProjectSource={openProjectSource}
            onOpenLegacy={() => setCenterMode("book")}
            onRuntimePrepared={prepared => setProjectRuntime(prepared)}
            onOpenRunControl={openPreparedRunControl}
          />
        ) : activeCenterMode === "memory" ? (
          <MemoryWorkspace docInfo={docInfo}
            profile={readOnly
              ? (docInfo?.metadata?.profile || docInfo?.metadata?.domain || (entities.length || relations.length ? "literary_v1" : "technical_d2l_v1"))
              : (projectRuntime?.selected_profile || docInfo?.metadata?.profile || docInfo?.metadata?.domain || (entities.length || relations.length ? "literary_v1" : "technical_d2l_v1"))}
            glossary={glossary} entities={entities} relations={relations} summaries={summaries}
            runMemory={thesisBaseDataset?.runMemory || null}
            blocks={blocks} chapters={chapters} activeBlock={block} initialKind={memoryFocusKind}
            evidenceLoading={registryOverlayLoading}
            onSelectBlock={selectBlock} onModeChange={setCenterMode} />
        ) : (
        <CenterEditor block={block} docInfo={docInfo} reviewed={!!review.blocks?.[selectedId]?.reviewed} spans={spans}
          editing={editing} mode={activeCenterMode}
          chapter={activeChapter} chapters={chapters} chapterBlocks={chapterBlocks} allBlocks={blocks} review={review} selectedId={selectedId}
          getSpansForBlock={getSpansForBlock} linkIndex={linkIndex} onSelectBlock={selectBlock} onNextUnreviewed={nextUnreviewedBlock}
          onEdit={() => setEditing(true)} onCommitClean={commitClean} onCancelEdit={() => setEditing(false)}
          onChangeType={changeType} onToggleOpening={() => toggleOpening(selectedId)} onToggleFlag={(flag) => toggleFlag(flag, selectedId)} onMarkReviewed={markReviewed}
          onAddGlossary={addGlossary} onAddEntity={addEntity} onPreviewRunChange={setCurrentPreviewRun} readOnly={readOnly}
          observability={thesisObservability}
          onConsoleBack={() => navigateView("workspace")}
          onOpenConsole={() => navigateView("console")}
          onOpenReport={() => navigateView("report")}
          runControl={{
            runtimeAvailable: !!runtimeJobId,
            sourceProjectId: activeDocId,
            sourceTitle: docInfo?.metadata?.title || docInfo?.doc_id || activeDocId,
            sourceStats: { chapters: chapters.length, blocks: blocks.length },
            jobId: runtimeJobId,
            runs: thesisRuns,
            selectedRunId,
            selectedRunLog,
            selectedRunEvents,
            blockPreview: runBlockPreview,
            watchlist: runWatchlist,
            reportSummary: runReportSummary,
            runForm,
            promptPreview: runPromptPreview,
            busy: runBusy,
            error: runError,
            onFormChange: updateRunForm,
            onPreview: previewThesisRun,
            onCreateRun: createThesisRun,
            onSelectRun: selectRun,
            onRefreshRuns: refreshThesisRuns,
            onOpenProjectSource: openProjectSource,
            onConfigurePipeline: openProjectPipelineModal,
            onPause: pauseRun,
            onCancel: cancelRun,
            onResume: resumeRun,
             onDich: thesisJobId(activeDocId) ? openDichModal : openProjectPipelineModal,
             locale: uiLocale,
             onLocaleChange: setUiLocale,
           }}
          selectedCallId={selectedCallId}
          selectedCallDetail={selectedCallDetail}
          callDetailLoading={callDetailLoading}
          onSelectCall={setSelectedCallId}
          focusTerm={focusedTermMeta}
          focusedTermId={focusedTermId}
          focusedTermIndex={focusedTermIndex}
          onFocusSpan={focusSpan}
          onClearFocus={clearFocusedTerm}
          onFocusJump={jumpFocusedTerm} />
        )}
        {runSurfaceView || ["memory", "structure"].includes(activeCenterMode) || !rightPanelOpen ? null : activeCenterMode === "preview" ? (
          <PreviewRightPanel docInfo={docInfo} block={block} preview={currentPreviewRun} />
        ) : (
          <RightPanel openTabs={rightOpenTabs} onToggleTab={toggleRightTab} counts={rpCounts}
            expanded={rightPanelExpanded} onToggleExpanded={toggleRightPanelExpanded}
            ctx={{ docInfo, terms: blockTerms, entities: blockEntities, allEntities: entities, relations: blockRelations, block, summary, references, evalOnly, thesisTranslations, readOnly, errors, stats, freezeReasons,
              currentMemory, projectMemory, currentScopeKind: contextScope.kind, currentScopeLabel: contextScope.label,
              memoryTotals: { glossary: glossary.length, entities: entities.length, relations: relations.length }, onOpenMemory: openMemory,
              schemaMigrating, onMigrateSchema: migrateSchema,
              onDeleteTerm: deleteTerm, onDeleteEntity: deleteEntity, onUpdateTerm: updateTerm, onUpdateEntity: updateEntity, onUpdateSummary: updateSummary,
              onCreateRelation: createRelation, onUpdateRelation: updateRelation, onDeleteRelation: deleteRelation,
              onUpdateBlockNotes: updateBlockNotes,
              onUpdateReference: updateReference, onCreateReference: createReferenceDraft, onSaveDraft: saveDraft, onMarkReviewedReference: markReviewedReference,
              onLockReference: lockReference, onUpdateDiscourse: updateDiscourse, onJump: jumpTo, onFocusTerm: focusTermById, history: historyState }} />
        )}
      </div>

      <Toasts items={toasts} onDismiss={dismiss} />
      {quickImportDialog}

      {modal?.kind === "project-pipeline" && (
        <Modal title={uiText("Cấu hình pipeline", "Configure pipeline")} icon={Ic.layers} className="pipeline-modal" onClose={() => !runBusy && setModal(null)}
          actions={<><button className="btn" disabled={runBusy} onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
            <button className="btn primary" disabled={runBusy || !(modal.chapters || []).length} onClick={confirmProjectPreflight}>
              {runBusy ? <span className="as-spin" /> : <Ic.play size={12} />} {uiText("Chạy kiểm tra 0-API", "Run 0-API check")}
            </button></>}>
          <p>
            {uiText("Chọn nhánh xử lý cho", "Choose a processing profile for")} <b>{docInfo?.metadata?.title || activeDocId}</b>. {uiText("Hệ thống tạo một snapshot/index bất biến có hash, sau đó chạy preflight thật và đưa log vào Console. Bước này", "The system creates an immutable hashed snapshot/index, runs the real preflight, and streams logs to Console. This step")} <b>{uiText("không gọi LLM/API", "does not call LLM/API")}</b>.
          </p>
          <div className="quick-import-profile pipeline-profile" role="group" aria-label={uiText("Chọn profile pipeline", "Choose pipeline profile")}>
            <button type="button" className={modal.profile === "technical_d2l_v1" ? "active" : ""}
              aria-pressed={modal.profile === "technical_d2l_v1"}
              onClick={() => setModal(current => ({ ...current, profile: "technical_d2l_v1" }))}>
              <Ic.layers size={14} /><span><b>{uiText("Tài liệu kỹ thuật", "Technical document")}</b><em>technical_d2l_v1 · {uiText("thuật ngữ", "terminology")}</em></span>
            </button>
            <button type="button" className={modal.profile === "literary_v1" ? "active" : ""}
              aria-pressed={modal.profile === "literary_v1"}
              onClick={() => setModal(current => ({ ...current, profile: "literary_v1" }))}>
              <Ic.book size={14} /><span><b>{uiText("Văn học", "Literary")}</b><em>literary_v1 · {uiText("nhân vật và mạch kể", "characters and narrative flow")}</em></span>
            </button>
          </div>
          <div className="pipeline-section-head">
            <span>{uiText("Chương / đơn vị sẽ kiểm tra", "Chapters / units to check")}</span>
            <span>
              <button type="button" onClick={() => setModal(current => ({ ...current, chapters: chapters.map(chapter => chapter.chapter_id).filter(Boolean) }))}>{uiText("Chọn tất cả", "Select all")}</button>
              <button type="button" onClick={() => setModal(current => ({ ...current, chapters: [] }))}>{uiText("Bỏ chọn", "Clear")}</button>
            </span>
          </div>
          <div className="pipeline-chapters">
            {chapters.map(chapter => {
              const checked = (modal.chapters || []).includes(chapter.chapter_id);
              const blockCount = blocks.filter(block => block.chapter_id === chapter.chapter_id).length;
              return (
                <label key={chapter.chapter_id} className={checked ? "selected" : ""}>
                  <input type="checkbox" checked={checked} onChange={() => toggleProjectPipelineChapter(chapter.chapter_id)} />
                  <span><b>{chapter.title || chapter.chapter_id}</b><em className="mono">{chapter.chapter_id} · {blockCount} block</em></span>
                </label>
              );
            })}
          </div>
          <div className="quick-import-note pipeline-snapshot-note">
            <Ic.lock size={12} /> {uiText("Dự án vẫn là nguồn chuẩn. Runtime chỉ giữ snapshot đã bỏ annotation và SQLite index riêng; sửa nguồn sau này sẽ tạo job_id mới thay vì ghi đè lần chạy cũ.", "The project remains the source of truth. Runtime keeps only an annotation-free snapshot and a separate SQLite index; later source edits create a new job_id instead of overwriting an old run.")}
          </div>
        </Modal>
      )}

      {modal?.kind === "export" && (
        <Modal title={uiText("Xuất dataset", "Export dataset")} icon={Ic.upload} onClose={() => setModal(null)}
          actions={<><button className="btn" onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
            <button className="btn primary" onClick={confirmExport}>{uiText("Xuất gói", "Export package")}</button></>}>
          <p>{uiText("Xuất gói dataset hiện tại. Đây có thể vẫn là gói làm việc nếu các gate kiểm tra hoặc duyệt chưa rõ ràng.", "Exports the current dataset package. It may still be a working package if validation or review gates are not clear.")}</p>
          <ul className="file-list">
            {["document.json","glossary.jsonl","entities.jsonl","entity_relations.jsonl","chapter_summaries.jsonl","manual_reference_subset.jsonl"].map(f =>
              <li key={f}><Ic.file size={12} /><span className="mono">{f}</span></li>)}
          </ul>
          <p className="muted">{errorCount > 0 ? <><Ic.alert size={11} /> {uiText(`Có ${errorCount} lỗi kiểm tra.`, `${errorCount} validation error(s) present.`)}</> : uiText("Báo cáo hiện tại không có lỗi kiểm tra.", "No validation errors in the current report.")}</p>
        </Modal>
      )}

      {modal?.kind === "freeze" && (
        <Modal title={uiText("Đóng băng dataset", "Freeze dataset")} icon={Ic.snow} onClose={() => setModal(null)}
          actions={<><button className="btn" onClick={() => setModal(null)}>{uiText("Đóng", "Close")}</button>
            <button className="btn primary" disabled={!freezeReady && !modal.serverReasons} onClick={confirmFreeze}><Ic.snow size={12} />{uiText("Đóng băng snapshot", "Freeze snapshot")}</button></>}>
          <p>{uiText("Đóng băng tạo một snapshot có phiên bản và đã kiểm tra. Thao tác bị chặn cho tới khi các gate kiểm tra, duyệt, reference, span, tóm tắt và nguồn gốc đều rõ ràng.", "Freeze creates a validated, versioned snapshot. It is blocked until validation, review, references, spans, summaries, and provenance gates are clear.")}</p>
          <div className="freeze-checks">
            {(modal.serverReasons || freezeReasons).length ? (modal.serverReasons || freezeReasons).map(reason => (
              <div key={reason} className="fc-row bad"><Ic.xCircle size={13} />{reason}</div>
            )) : <div className="fc-row ok"><Ic.checkCircle size={13} />{uiText("Mọi gate đóng băng đều đạt.", "All freeze gates are clear.")}</div>}
          </div>
        </Modal>
      )}

      {modal?.kind === "dich-run" && (
        <Modal title={uiText("Dịch — một nút", "Translate — one button")} icon={Ic.play} onClose={() => setModal(null)}
          actions={<><button className="btn" onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
            <button className="btn primary" disabled={runBusy || !(dichForm.chapters || "").trim()} onClick={confirmDich}><Ic.play size={12} />{uiText("Bắt đầu dịch (API thật)", "Start translation (real API)")}</button></>}>
          <p>{uiText("Chạy toàn bộ pipeline một lượt (Builder → Auditor → Translator → chấm điểm → báo cáo) cho dataset", "Runs the full pipeline in one pass (Builder → Auditor → Translator → scoring → report) for dataset")} <span className="mono">{modal.jobId}</span>. {uiText("Tiến trình hiện trực tiếp trong Console; có thể Tạm dừng/Tiếp tục/Hủy bất kỳ lúc nào.", "Progress appears live in Console; you can Pause/Resume/Cancel at any time.")}</p>
          <div className="run-grid">
            <label><span>{uiText("chương", "chapters")}</span><input value={dichForm.chapters} onChange={e => setDichForm(f => ({ ...f, chapters: e.target.value }))} placeholder="d2l_preface" /></label>
            <label><span>{uiText("hồ sơ", "profile")}</span><input value={dichForm.profile} onChange={e => setDichForm(f => ({ ...f, profile: e.target.value }))} placeholder="technical_d2l_v1" /></label>
            <label><span>{uiText("db nguồn (đã đóng băng, đọc mode=ro)", "source DB (frozen, read mode=ro)")}</span><input value={dichForm.db} onChange={e => setDichForm(f => ({ ...f, db: e.target.value }))} placeholder="data/jobs/d2l_p1/memory.sqlite3" /></label>
            <label><span>{uiText("trần ngân sách ($)", "budget cap ($)")}</span><input type="number" step="0.1" min="0.01" value={dichForm.budget_cap_usd} onChange={e => setDichForm(f => ({ ...f, budget_cap_usd: e.target.value }))} /></label>
          </div>
          <label style={{display:"flex",alignItems:"center",gap:"8px",marginTop:"6px",cursor:"pointer"}}>
            <input type="checkbox" checked={!!dichForm.with_s0} onChange={e => setDichForm(f => ({ ...f, with_s0: e.target.checked }))} />
            <span>{uiText("Kèm baseline", "Include baseline")} <span className="mono">S0</span> {uiText("(chạy S0+S1 để so sánh S0↔S1 — tốn thêm API)", "(run S0+S1 for an S0↔S1 comparison — uses additional API)")}</span>
          </label>
          <p className="muted"><Ic.alert size={11} /> {uiText("Chạy API thật. Gate ngân sách sẽ tự tạm dừng nếu ước tính lũy kế vượt", "Uses the real API. The budget gate pauses automatically if the cumulative estimate exceeds")} <span className="mono">${Number(dichForm.budget_cap_usd || 1.5).toFixed(2)}</span>. {uiText("Chi phí trong Console là TRẦN trên — thực tế thường thấp hơn nhiều (cascade T3 chạy Gemma local ~$0).", "Console cost is an UPPER BOUND — actual cost is usually much lower (cascade T3 runs local Gemma at ~$0).")}</p>
        </Modal>
      )}

      {modal?.kind === "cancel-run" && (
        <Modal title={uiText("Hủy lần chạy", "Cancel run")} icon={Ic.alert} tone="bad" onClose={() => setModal(null)}
          actions={<><button className="btn" onClick={() => setModal(null)}>{uiText("Không hủy", "Keep run")}</button>
            <button className="btn primary" onClick={confirmCancelRun}><Ic.alert size={12} />{uiText("Hủy lần chạy", "Cancel run")}</button></>}>
          <p>{uiText("Dừng cứng tiến trình đang chạy (taskkill toàn bộ cây tiến trình). Các tầng đã hoàn tất vẫn được giữ trong manifest — có thể tiếp tục lại từ checkpoint sau này.", "Force-stops the running process (taskkill on the full process tree). Completed stages remain in the manifest and the run can later resume from a checkpoint.")}</p>
          <p className="mono">{modal.runId}</p>
        </Modal>
      )}

      {modal?.kind === "resume-run" && (() => {
        const stages = modal.estimate?.estimate_by_stage || [];
        const costOf = (s) => {
          const v = s?.cost_cap_usd ?? s?.estimate_usd ?? s?.cost_usd ?? s?.budget_usd;
          return typeof v === "number" ? `$${v.toFixed(4)}` : "";
        };
        return (
          <Modal title={uiText("Tiếp tục lần chạy", "Resume run")} icon={Ic.play} onClose={() => setModal(null)}
            actions={<><button className="btn" onClick={() => setModal(null)}>{uiText("Không", "No")}</button>
              <button className="btn primary" onClick={confirmResumeRun}><Ic.play size={12} />{uiText("Tiếp tục (API thật)", "Resume (real API)")}</button></>}>
            <p>{uiText("Chạy tiếp từ tầng đã checkpoint bằng argv gốc (bỏ", "Continues from the checkpointed stage using the original argv (removes")} <span className="mono">--estimate-only</span>, {uiText("thêm", "adds")} <span className="mono">--resume</span>). {uiText("Xác nhận này gắn với đúng argv digest của lần chạy.", "This confirmation is bound to the run's exact argv digest.")}</p>
            {stages.length ? (
              <ul className="file-list">
                {stages.map((s, i) => (
                  <li key={i}><Ic.file size={12} /><span className="mono">{s.stage || s.name || `stage ${i + 1}`}</span><span className="muted">{costOf(s)}</span></li>
                ))}
              </ul>
            ) : (
              <p className="muted">{uiText("Manifest chưa có ước tính theo tầng; orchestrator vẫn áp budget gate nội bộ khi chạy.", "The manifest has no per-stage estimate; the orchestrator still applies its internal budget gate during the run.")}</p>
            )}
            <p className="mono">{modal.runId}</p>
          </Modal>
        );
      })()}

      {modal?.kind === "delete-term" && (() => {
        const term = modal.term;
        const locked = ["locked", "human_verified"].includes(term?.status);
        const occCount = (term?.occurrences || []).length;
        const blockCount = new Set((term?.occurrences || []).map(o => o.block_id)).size;
        return (
          <Modal title={uiText("Xóa thuật ngữ", "Delete glossary term")} icon={Ic.trash} tone="bad" onClose={() => setModal(null)}
            actions={<><button className="btn" onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
              <button className="btn danger" disabled={locked} onClick={confirmDeleteTerm}>{uiText("Xóa thuật ngữ", "Delete term")}</button></>}>
            {locked ? (
              <p><b>{term.source_term}</b> {uiText(`đang ở trạng thái ${term.status}. Hãy mở khóa hoặc hạ trạng thái trước khi xóa.`, `is ${term.status}. Unlock or downgrade it before deleting.`)}</p>
            ) : (
              <>
                <p>{uiText("Xóa", "Delete")} <b>{term.source_term}</b> {uiText("và gỡ toàn bộ dấu vết chú giải của nó khỏi tài liệu?", "and remove its annotation footprint from the document?")}</p>
                <p className="muted">{uiText(`Sẽ xóa ${occCount} lần xuất hiện trên ${blockCount || 0} block. Có thể hoàn tác.`, `${occCount} occurrence(s) across ${blockCount || 0} block(s) will be removed. This is undoable.`)}</p>
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
          <Modal title={uiText("Xóa thực thể", "Delete entity")} icon={Ic.trash} tone="bad" onClose={() => setModal(null)}
            actions={<><button className="btn" onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
              <button className="btn danger" onClick={confirmDeleteEntity}>{uiText("Xóa thực thể", "Delete entity")}</button></>}>
            <p>{uiText("Xóa", "Delete")} <b>{entity.canonical_source || entity.entity_id}</b> {uiText("và gỡ dấu vết lần nhắc của nó khỏi tài liệu?", "and remove its own mention footprint from the document?")}</p>
            <p className="muted">{uiText(`Sẽ xóa ${mentionCount} lần nhắc trên ${blockCount || 0} block. Có thể hoàn tác.`, `${mentionCount} mention(s) across ${blockCount || 0} block(s) will be removed. This is undoable.`)}</p>
            <p className="muted">{uiText("Nếu thực thể được dùng trong discourse, tóm tắt chương hoặc quan hệ, thao tác xóa sẽ bị chặn và phải gỡ các reference đó trước.", "If this entity is used by discourse, chapter summary, or relation, deletion will be blocked and those references must be removed first.")}</p>
          </Modal>
        );
      })()}

      {modal?.kind === "delete-relation" && (() => {
        const relation = modal.relation;
        return (
          <Modal title={uiText("Xóa quan hệ thực thể", "Delete entity relation")} icon={Ic.trash} tone="bad" onClose={() => setModal(null)}
            actions={<><button className="btn" onClick={() => setModal(null)}>{uiText("Hủy", "Cancel")}</button>
              <button className="btn danger" onClick={confirmDeleteRelation}>{uiText("Xóa quan hệ", "Delete relation")}</button></>}>
            <p>{uiText("Xóa quan hệ", "Delete relation")} <b>{relation.relation_id}</b>?</p>
            <p className="muted">{uiText("Thao tác chỉ xóa quan hệ address-policy cấp tài liệu. Các lần nhắc thực thể, thuật ngữ, discourse và tóm tắt được giữ lại. Có thể hoàn tác.", "This removes only the document-level address-policy relation. Entity mentions, glossary, discourse, and summaries are kept. This is undoable.")}</p>
          </Modal>
        );
      })()}

      {modal?.kind === "delete-blocked" && (
        <Modal title={modal.title || uiText("Không thể xóa", "Delete blocked")} icon={Ic.alert} tone="bad" onClose={() => setModal(null)}
          actions={<button className="btn" onClick={() => setModal(null)}>{uiText("Đóng", "Close")}</button>}>
          <p>{modal.message || uiText("Mục này vẫn đang được tham chiếu.", "This item is still referenced.")}</p>
          {(modal.references || []).length ? (
            <ul className="file-list">
              {modal.references.map((ref, index) => <li key={index}><Ic.link size={12} /><span className="mono">{describeExternalRef(ref)}</span></li>)}
            </ul>
          ) : <p className="muted">{uiText("Hãy gỡ các reference liên quan trước rồi thử lại.", "Remove related references first, then try again.")}</p>}
        </Modal>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
